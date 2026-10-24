#!/usr/bin/env python3
"""
Mod日本語未翻訳ファインダー
GitHub上のMinecraft modリポジトリを検索し、
assets/<modid>/lang/ に en_us はあるが ja_jp が無い(または一部しか翻訳されていない)modを一覧表示します。

必要なもの: Python 3.8+ (標準ライブラリのみ。追加インストール不要)
推奨: GitHub Personal Access Token (権限なしでOK)。
      未設定だと API 制限(検索10回/分・その他60回/時)で、ほとんど調べられません。
      環境変数 GITHUB_TOKEN に入れるか、画面の「トークン」欄に貼り付けてください。
"""
import base64
import csv
import json
import os
import re
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

API = "https://api.github.com"
STATE_FILE = Path(__file__).with_name("mod_ja_finder_state.json")  # 作業中のフォーク/ブランチの記録
CLIENT_ID = ""  # GitHub OAuth App の Client ID (Device Flow有効)。環境変数 GITHUB_CLIENT_ID でも可
EN_RE = re.compile(r"^(?:.*/)?assets/([^/]+)/lang/(en_us\.json|en_us\.lang)$", re.I)
JA_RE = re.compile(r"^(?:.*/)?assets/([^/]+)/lang/(ja_jp\.json|ja_jp\.lang)$", re.I)


class RateLimit(Exception):
    pass


class GitHub:
    def __init__(self, token, log):
        self.token = token.strip()
        self.log = log

    def _req(self, url, raw=False):
        headers = {"User-Agent": "mod-ja-finder", "Accept": "application/vnd.github+json"}
        if self.token and not raw:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    return data if raw else json.loads(data.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and not raw:
                    remain = e.headers.get("X-RateLimit-Remaining")
                    reset = e.headers.get("X-RateLimit-Reset")
                    retry = e.headers.get("Retry-After")
                    wait = None
                    if retry:
                        wait = int(retry) + 1
                    elif remain == "0" and reset:
                        wait = max(1, int(reset) - int(time.time()) + 1)
                    if wait is not None and wait <= 75 and attempt < 2:
                        self.log(f"API制限のため {wait} 秒待機します…")
                        time.sleep(wait)
                        continue
                    raise RateLimit("GitHub APIの制限に達しました。トークンを設定するか、しばらく待ってください。")
                if e.code in (404, 409, 451):
                    return None
                raise
            except urllib.error.URLError:
                if attempt == 2:
                    raise
                time.sleep(2)

    def search_repos(self, keyword, loader, min_stars, page):
        q = ["topic:minecraft-mod", f"stars:>={min_stars}", "archived:false"]
        if loader != "指定なし":
            q.append(f"topic:{loader}")
        if keyword:
            q.append(keyword)
        url = f"{API}/search/repositories?q={urllib.parse.quote(' '.join(q))}&sort=stars&order=desc&per_page=30&page={page}"
        res = self._req(url)
        return (res or {}).get("items", [])

    def tree(self, full, branch):
        return self._req(f"{API}/repos/{full}/git/trees/{urllib.parse.quote(branch)}?recursive=1")

    def raw(self, full, branch, path):
        url = f"https://raw.githubusercontent.com/{full}/{branch}/{urllib.parse.quote(path)}"
        try:
            return self._req(url, raw=True).decode("utf-8-sig", "replace")
        except Exception:
            return None


def count_keys(text, path):
    if text is None:
        return None
    if path.lower().endswith(".json"):
        try:
            d = json.loads(text)
            return len(d) if isinstance(d, dict) else None
        except Exception:
            return None
    return sum(1 for ln in text.splitlines() if "=" in ln and not ln.lstrip().startswith("#"))


def _post(url, data=None, token="", form=False):
    headers = {"User-Agent": "mod-ja-finder", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Accept"] = "application/json"
        else:
            body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body if body is not None else b"", headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def device_login(client_id, on_code, should_stop):
    """GitHub Device Flow。on_code(user_code, url) でユーザーに入力コードを見せる。成功時トークンを返す。"""
    d = _post("https://github.com/login/device/code",
              {"client_id": client_id, "scope": "public_repo"}, form=True)
    on_code(d["user_code"], d["verification_uri"])
    interval = d.get("interval", 5)
    deadline = time.time() + d.get("expires_in", 900)
    while time.time() < deadline and not should_stop():
        time.sleep(interval)
        r = _post("https://github.com/login/oauth/access_token",
                  {"client_id": client_id, "device_code": d["device_code"],
                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True)
        if "access_token" in r:
            return r["access_token"]
        err = r.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            raise RuntimeError(err or "ログインに失敗しました")
    raise RuntimeError("ログインがタイムアウトしました")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code} {msg}")
        self.code = code


def call(method, url, token, data=None):
    headers = {"User-Agent": "mod-ja-finder", "Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {token}"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8")
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            msg = ""
        raise ApiError(e.code, msg)


def get_file(repo, path, ref, token):
    d = call("GET", f"{API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}", token)
    if not d.get("content"):
        raise RuntimeError(f"{path} を取得できません(1MB超?)")
    return base64.b64decode(d["content"]).decode("utf-8-sig", "replace"), d["sha"]


def merge_missing(en_text, ja_text, path):
    """ja側に無いキーを英語のまま追記。追記不要なら None。"""
    if path.lower().endswith(".json"):
        en, ja = json.loads(en_text), json.loads(ja_text)
        missing = {k: v for k, v in en.items() if k not in ja}
        if not missing:
            return None
        ja.update(missing)
        return json.dumps(ja, ensure_ascii=False, indent=2) + "\n"
    have = {ln.split("=", 1)[0].strip() for ln in ja_text.splitlines() if "=" in ln}
    add = [ln for ln in en_text.splitlines()
           if "=" in ln and not ln.lstrip().startswith("#") and ln.split("=", 1)[0].strip() not in have]
    if not add:
        return None
    return ja_text.rstrip("\n") + "\n" + "\n".join(add) + "\n"


def load_jobs():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_jobs(jobs):
    STATE_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_job(tok, meta, modid, log):
    """フォーク → ブランチ作成 → ひな形(ja_jp)をコミット。作業情報(job)を返す。"""
    upstream, base, enpath, japath = meta["upstream"], meta["branch"], meta["en"], meta.get("ja")
    safe = re.sub(r"[^\w.-]", "-", modid)
    log(f"{upstream}: フォーク中…")
    fork = call("POST", f"{API}/repos/{upstream}/forks", tok, {})["full_name"]
    sha = None
    for _ in range(30):  # フォーク完成待ち
        try:
            sha = call("GET", f"{API}/repos/{fork}/git/ref/heads/{base}", tok)["object"]["sha"]
            break
        except ApiError:
            time.sleep(2)
    if not sha:
        raise RuntimeError("フォークの準備が完了しませんでした。少し待って再実行してください。")
    try:  # 既存フォークなら本家に追従
        call("POST", f"{API}/repos/{fork}/merge-upstream", tok, {"branch": base})
        sha = call("GET", f"{API}/repos/{fork}/git/ref/heads/{base}", tok)["object"]["sha"]
    except ApiError:
        pass
    branch = ("update-ja-jp-" if japath else "add-ja-jp-") + safe
    try:
        call("POST", f"{API}/repos/{fork}/git/refs", tok, {"ref": f"refs/heads/{branch}", "sha": sha})
    except ApiError as e:
        if e.code != 422:  # 422 = 既にブランチあり(再実行)
            raise
    en_text, _ = get_file(fork, enpath, branch, tok)
    folder = enpath.rsplit("/", 1)[0]
    if japath:  # 一部未翻訳: 足りないキーを英語のまま追記
        outpath = japath
        ja_text, ja_sha = get_file(fork, japath, branch, tok)
        new_text = merge_missing(en_text, ja_text, japath)
        msg = f"Add missing keys to ja_jp for {modid}"
    else:  # 完全未翻訳: 英語ファイルをそのままひな形に
        outpath = folder + ("/ja_jp.json" if enpath.lower().endswith(".json") else "/ja_JP.lang")
        try:
            get_file(fork, outpath, branch, tok)
            new_text, ja_sha = None, None  # 既にある(翻訳中のかもしれない)ので上書きしない
        except ApiError:
            new_text, ja_sha = en_text, None
        msg = f"Add ja_jp template for {modid}"
    if new_text is not None:
        body = {"message": msg, "branch": branch,
                "content": base64.b64encode(new_text.encode("utf-8")).decode()}
        if ja_sha:
            body["sha"] = ja_sha
        call("PUT", f"{API}/repos/{fork}/contents/{urllib.parse.quote(outpath)}", tok, body)
    job = {"upstream": upstream, "fork": fork, "branch": branch, "base": base, "modid": modid,
           "path": outpath, "enpath": enpath, "partial": bool(japath)}
    jobs = [j for j in load_jobs() if not (j["upstream"] == upstream and j["modid"] == modid)]
    jobs.append(job)
    save_jobs(jobs)
    return job


def untranslated_ratio(en_text, ja_text, path):
    try:
        if path.lower().endswith(".json"):
            en, ja = json.loads(en_text), json.loads(ja_text)
            same = sum(1 for k, v in ja.items() if en.get(k) == v)
            return same / max(1, len(ja))
        pairs = lambda t: dict(ln.split("=", 1) for ln in t.splitlines() if "=" in ln and not ln.lstrip().startswith("#"))
        en, ja = pairs(en_text), pairs(ja_text)
        return sum(1 for k, v in ja.items() if en.get(k) == v) / max(1, len(ja))
    except Exception:
        return 0.0


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mod日本語未翻訳ファインダー")
        self.geometry("1100x640")
        self.stop_flag = False
        self.rows = []
        self.meta = {}
        self._build()

    def _build(self):
        f = ttk.Frame(self, padding=8)
        f.pack(fill="x")
        self.kw = tk.StringVar()
        self.loader = tk.StringVar(value="指定なし")
        self.stars = tk.StringVar(value="20")
        self.maxrepo = tk.StringVar(value="60")
        self.token = tk.StringVar(value=os.environ.get("GITHUB_TOKEN", ""))
        self.partial = tk.BooleanVar(value=True)
        self.show_partial = tk.BooleanVar(value=True)

        ttk.Label(f, text="キーワード").grid(row=0, column=0, sticky="e")
        ttk.Entry(f, textvariable=self.kw, width=22).grid(row=0, column=1, padx=4)
        ttk.Label(f, text="ローダー").grid(row=0, column=2, sticky="e")
        ttk.Combobox(f, textvariable=self.loader, width=10, state="readonly",
                     values=["指定なし", "forge", "fabric", "neoforge", "quilt"]).grid(row=0, column=3, padx=4)
        ttk.Label(f, text="最小スター").grid(row=0, column=4, sticky="e")
        ttk.Entry(f, textvariable=self.stars, width=6).grid(row=0, column=5, padx=4)
        ttk.Label(f, text="最大リポジトリ数").grid(row=0, column=6, sticky="e")
        ttk.Entry(f, textvariable=self.maxrepo, width=6).grid(row=0, column=7, padx=4)

        ttk.Label(f, text="トークン").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Entry(f, textvariable=self.token, width=48, show="*").grid(row=1, column=1, columnspan=4, sticky="w", padx=4)
        ttk.Checkbutton(f, text="翻訳漏れ(キー数不足)も調べる", variable=self.partial).grid(row=1, column=5, columnspan=3, sticky="w")

        b = ttk.Frame(self, padding=(8, 0))
        b.pack(fill="x")
        self.btn = ttk.Button(b, text="検索開始", command=self.start)
        self.btn.pack(side="left")
        ttk.Button(b, text="停止", command=lambda: setattr(self, "stop_flag", True)).pack(side="left", padx=4)
        ttk.Button(b, text="CSV保存", command=self.export).pack(side="left")
        ttk.Button(b, text="GitHubでログイン", command=self.login).pack(side="left", padx=(16, 4))
        ttk.Button(b, text="フォーク+ひな形作成", command=self.prepare_selected).pack(side="left")
        ttk.Button(b, text="翻訳作業一覧 / PR", command=self.jobs_window).pack(side="left", padx=4)
        ttk.Checkbutton(b, text="一部未翻訳も表示", variable=self.show_partial, command=self.refresh).pack(side="left", padx=12)
        ttk.Label(b, text="ダブルクリックでGitHubを開く").pack(side="right")

        cols = ("status", "repo", "modid", "stars", "keys", "url")
        self.tv = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w in [("status", "状態", 90), ("repo", "リポジトリ", 280), ("modid", "modid", 140),
                        ("stars", "★", 60), ("keys", "ja/en キー数", 100), ("url", "lang フォルダ", 380)]:
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w")
        self.tv.pack(fill="both", expand=True, padx=8, pady=6)
        self.tv.bind("<Double-1>", self.open_row)

        self.status = tk.StringVar(value="待機中")
        ttk.Label(self, textvariable=self.status, padding=(8, 2)).pack(fill="x")

    def log(self, msg):
        self.after(0, lambda: self.status.set(msg))

    def start(self):
        try:
            stars = int(self.stars.get())
            maxrepo = int(self.maxrepo.get())
        except ValueError:
            messagebox.showerror("入力エラー", "スターと最大リポジトリ数は数字で入力してください。")
            return
        if not self.token.get().strip():
            if not messagebox.askyesno("トークン未設定", "トークン無しだと数件しか調べられません。続行しますか？"):
                return
        self.rows.clear()
        self.meta.clear()
        self.refresh()
        self.stop_flag = False
        self.btn.state(["disabled"])
        threading.Thread(target=self.worker, args=(stars, maxrepo), daemon=True).start()

    def worker(self, stars, maxrepo):
        gh = GitHub(self.token.get(), self.log)
        checked = 0
        try:
            page = 1
            while checked < maxrepo and not self.stop_flag:
                items = gh.search_repos(self.kw.get().strip(), self.loader.get(), stars, page)
                if not items:
                    break
                for repo in items:
                    if checked >= maxrepo or self.stop_flag:
                        break
                    checked += 1
                    self.log(f"[{checked}/{maxrepo}] {repo['full_name']} を調査中…")
                    self.inspect(gh, repo)
                page += 1
                if page > 34:  # 検索APIは最大1000件
                    break
                time.sleep(2)
            self.log(f"完了: {checked} リポジトリを調査 / 該当 {len(self.rows)} 件")
        except RateLimit as e:
            self.log(str(e))
        except Exception as e:
            self.log(f"エラー: {e}")
        finally:
            self.after(0, lambda: self.btn.state(["!disabled"]))

    def inspect(self, gh, repo):
        full, branch = repo["full_name"], repo.get("default_branch", "main")
        t = gh.tree(full, branch)
        if not t or "tree" not in t:
            return
        en, ja = {}, {}
        for node in t["tree"]:
            if node.get("type") != "blob":
                continue
            p = node["path"]
            m = EN_RE.match(p)
            if m:
                en[m.group(1).lower()] = p
                continue
            m = JA_RE.match(p)
            if m:
                ja[m.group(1).lower()] = p
        for modid, enpath in en.items():
            folder = enpath.rsplit("/", 1)[0]
            url = f"https://github.com/{full}/tree/{branch}/{folder}"
            if modid not in ja:
                self.add(("未翻訳", full, modid, repo["stargazers_count"], "- / -", url),
                         dict(upstream=full, branch=branch, en=enpath, ja=None))
            elif self.partial.get():
                e_n = count_keys(gh.raw(full, branch, enpath), enpath)
                j_n = count_keys(gh.raw(full, branch, ja[modid]), ja[modid])
                if e_n and j_n is not None and j_n < e_n * 0.9:
                    self.add(("一部未翻訳", full, modid, repo["stargazers_count"], f"{j_n} / {e_n}", url),
                             dict(upstream=full, branch=branch, en=enpath, ja=ja[modid]))

    def add(self, row, meta):
        self.meta[(row[1], str(row[2]))] = meta
        self.rows.append(row)
        self.after(0, self.refresh)

    def refresh(self):
        self.tv.delete(*self.tv.get_children())
        for r in self.rows:
            if r[0] == "一部未翻訳" and not self.show_partial.get():
                continue
            self.tv.insert("", "end", values=r)

    # ---------- ログイン / フォーク ----------
    def login(self):
        cid = os.environ.get("GITHUB_CLIENT_ID", "").strip() or CLIENT_ID
        if not cid:
            messagebox.showinfo(
                "Client IDが必要です",
                "GitHubの Settings > Developer settings > OAuth Apps でアプリを作成し、\n"
                "「Enable Device Flow」にチェックを入れて Client ID を取得してください。\n"
                "それをこのファイルの CLIENT_ID に書くか、環境変数 GITHUB_CLIENT_ID に設定します。\n"
                "(面倒な場合は、トークン欄に Personal Access Token を貼ってもフォークできます)")
            return
        self._login_stop = False

        def on_code(code, url):
            self.after(0, lambda: (webbrowser.open(url),
                                   self.status.set(f"ブラウザで次のコードを入力してください: {code}"),
                                   self.clipboard_clear(), self.clipboard_append(code)))

        def run():
            try:
                tok = device_login(cid, on_code, lambda: self._login_stop)
                self.after(0, lambda: (self.token.set(tok), self.status.set("ログイン成功！")))
            except Exception as e:
                self.log(f"ログイン失敗: {e}")

        threading.Thread(target=run, daemon=True).start()

    def prepare_selected(self):
        sel = self.tv.selection()
        tok = self.token.get().strip()
        if not sel:
            messagebox.showinfo("ひな形作成", "対象の行を選択してください。")
            return
        if not tok:
            messagebox.showinfo("ひな形作成", "先にログイン(またはトークン入力)をしてください。")
            return
        items = [(self.tv.item(i)["values"][1], str(self.tv.item(i)["values"][2])) for i in sel]
        if not messagebox.askyesno(
                "確認", f"{len(items)} 件を自分のアカウントにフォークし、ブランチを作って\n"
                        "ja_jp のひな形(英語のまま)をコミットします。\n\n" +
                        "\n".join(f"{r} [{m}]" for r, m in items[:10])):
            return

        def run():
            ok = 0
            for repo, modid in items:
                meta = self.meta.get((repo, modid))
                if not meta:
                    continue
                try:
                    prepare_job(tok, meta, modid, self.log)
                    ok += 1
                    self.log(f"ひな形作成完了: {repo} [{modid}]")
                except Exception as e:
                    self.log(f"失敗 {repo} [{modid}]: {e}")
                time.sleep(2)
            self.log(f"ひな形作成: {ok}/{len(items)} 件成功。「翻訳作業一覧 / PR」から編集・PR作成できます。")

        threading.Thread(target=run, daemon=True).start()

    def jobs_window(self):
        win = tk.Toplevel(self)
        win.title("翻訳作業一覧")
        win.geometry("900x380")
        cols = ("upstream", "modid", "branch", "path")
        tv = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
        for c, t, w in [("upstream", "本家リポジトリ", 240), ("modid", "modid", 120),
                        ("branch", "ブランチ", 200), ("path", "ファイル", 320)]:
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        tv.pack(fill="both", expand=True, padx=8, pady=8)

        def reload():
            tv.delete(*tv.get_children())
            for j in load_jobs():
                tv.insert("", "end", values=(j["upstream"], j["modid"], j["branch"], j["path"]))

        def current():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("翻訳作業", "行を選択してください。", parent=win)
                return None
            v = tv.item(sel[0])["values"]
            for j in load_jobs():
                if j["upstream"] == v[0] and str(j["modid"]) == str(v[1]):
                    return j

        def edit():
            j = current()
            if j:
                webbrowser.open(f"https://github.com/{j['fork']}/edit/{j['branch']}/{j['path']}")

        def make_pr():
            j = current()
            if not j:
                return
            tok = self.token.get().strip()
            if not tok:
                messagebox.showinfo("PR", "先にログインしてください。", parent=win)
                return
            try:
                en, _ = get_file(j["fork"], j["enpath"], j["branch"], tok)
                ja, _ = get_file(j["fork"], j["path"], j["branch"], tok)
                ratio = untranslated_ratio(en, ja, j["path"])
                if ratio > 0.3 and not messagebox.askyesno(
                        "未翻訳が残っています",
                        f"英語のままのキーが約 {ratio:.0%} 残っています。\nこのままPRを送りますか？", parent=win):
                    return
                if not messagebox.askyesno(
                        "PR作成の確認",
                        f"{j['upstream']} に Pull Request を送ります。\n"
                        f"{j['fork'].split('/')[0]}:{j['branch']} → {j['base']}\n\nよろしいですか？", parent=win):
                    return
                verb = "Update" if j["partial"] else "Add"
                pr = call("POST", f"{API}/repos/{j['upstream']}/pulls", tok, {
                    "title": f"{verb} Japanese (ja_jp) translation for {j['modid']}",
                    "head": f"{j['fork'].split('/')[0]}:{j['branch']}",
                    "base": j["base"],
                    "body": f"{verb} Japanese translation (`{j['path']}`).\n\n"
                            "日本語翻訳を追加/更新しました。ご確認よろしくお願いします。",
                    "maintainer_can_modify": True})
                webbrowser.open(pr["html_url"])
                self.status.set(f"PR作成: {pr['html_url']}")
            except ApiError as e:
                messagebox.showerror("PR失敗", f"{e}\n(既にPR済み、差分なし、またはPR受付不可の可能性があります)", parent=win)
            except Exception as e:
                messagebox.showerror("PR失敗", str(e), parent=win)

        def remove():
            j = current()
            if j:
                save_jobs([x for x in load_jobs() if not (x["upstream"] == j["upstream"] and x["modid"] == j["modid"])])
                reload()

        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="GitHubで編集(翻訳する)", command=edit).pack(side="left")
        ttk.Button(bar, text="Pull Request作成", command=make_pr).pack(side="left", padx=6)
        ttk.Button(bar, text="一覧から削除", command=remove).pack(side="left")
        ttk.Label(bar, text="翻訳は自分のフォークのブランチ上で行い、終わったらPR作成").pack(side="right")
        reload()

    def open_row(self, _):
        sel = self.tv.selection()
        if sel:
            webbrowser.open(self.tv.item(sel[0])["values"][5])

    def export(self):
        if not self.rows:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["状態", "リポジトリ", "modid", "stars", "ja/enキー数", "URL"])
                w.writerows(self.rows)


if __name__ == "__main__":
    App().mainloop()
