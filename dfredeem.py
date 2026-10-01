#!/usr/bin/env python3
"""
dfredeem - Delta Force (Garena) redeem TUI

  pip install playwright rich
  python dfredeem.py        (doc codes.txt, tu mo Brave/Chrome/Edge)

Keys: [SPACE] pause/resume  [S] skip wait  [Q] quit
Captcha / rate limit: script pauses, you solve it by hand, then SPACE.
"""
import argparse, csv, json, os, random, re, socket, subprocess, sys, threading, time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

DEFAULT_URL = "https://redeem.df.garena.sg/vi/cdkgarena.html"

# ordered: first match wins. Sua/them keyword theo message thuc te cua trang.
RULES = [
    ("CAPTCHA",   r"captcha|xác minh|verify|robot"),
    ("USED",      r"redemption limit|limit of cdkey|đạt giới hạn|vượt quá giới hạn|đã (được )?(sử dụng|dùng)|already (used|redeemed)"),
    ("RATELIMIT", r"quá nhiều|too many|thử lại sau|try again later|frequent|rate limit|too fast"),
    ("EXPIRED",   r"hết hạn|expired"),
    ("INVALID",   r"không (hợp lệ|tồn tại|chính xác|đúng)|invalid|incorrect|not exist|\bsai\b"),
    ("SUCCESS",   r"thành công|success|congrat|chúc mừng|đã nhận"),
]
FINAL = {"SUCCESS", "USED", "INVALID", "EXPIRED"}
STYLE = {"SUCCESS": "bold green", "USED": "yellow", "INVALID": "red", "EXPIRED": "magenta",
         "RATELIMIT": "bold red", "CAPTCHA": "bold cyan", "UNKNOWN": "dim", "NO_RESPONSE": "dim"}
ICON = {"SUCCESS": "✔", "USED": "●", "INVALID": "✘", "EXPIRED": "◌",
        "RATELIMIT": "⏳", "CAPTCHA": "🧩", "UNKNOWN": "?", "NO_RESPONSE": "…"}
NOISE = re.compile(r"google-analytics|googletagmanager|doubleclick|sentry|facebook\.|hotjar|/collect|beacon", re.I)
MSG_KEYS = ("msg", "message", "error_msg", "error", "desc", "detail", "reason")


# ---------------------------------------------------------------- keys
class Keys:
    def __init__(self):
        self.q, self._stop = deque(), False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        if os.name == "nt":
            import msvcrt
            while not self._stop:
                if msvcrt.kbhit():
                    self.q.append(msvcrt.getwch().lower())
                time.sleep(0.03)
        else:
            import select, termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not self._stop:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        self.q.append(sys.stdin.read(1).lower())
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def get(self):
        return self.q.popleft() if self.q else None

    def stop(self):
        self._stop = True
        self.t.join(timeout=0.5)


# ---------------------------------------------------------------- state
class State:
    def __init__(self, total, profile):
        self.total, self.profile = total, profile
        self.done, self.current, self.status = 0, "-", "STARTING"
        self.counts, self.rows = Counter(), deque(maxlen=200)
        self.paused = self.quit = self.skip = False
        self.countdown, self.strikes, self.bad = 0.0, 0, 0
        self.t0, self.durations = time.time(), deque(maxlen=20)


def classify(text):
    t = text.lower()
    for name, pat in RULES:
        if re.search(pat, t):
            return name
    return "UNKNOWN"


# ---------------------------------------------------------------- sniffer
def find_msg(obj):
    if isinstance(obj, dict):
        for k in MSG_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            r = find_msg(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_msg(v)
            if r:
                return r
    return None


class Sniffer:
    """Bat response XHR/fetch de doc ket qua redeem tu API. Moi response ghi vao debug.log."""
    def __init__(self, debug_path="debug.log"):
        self.resps, self.debug, self.dumped = [], Path(debug_path), False

    def on_response(self, r):
        try:
            if r.request.resource_type in ("xhr", "fetch") and not NOISE.search(r.url):
                self.resps.append(r)
        except Exception:
            pass

    def clear(self):
        self.resps.clear()

    def messages(self, code):
        out = []
        for r in list(self.resps):
            try:
                raw = r.text()
            except Exception:
                raw = ""
            with self.debug.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%H:%M:%S}] {code} {r.request.method} {r.status} {r.url}\n"
                        f"    {raw[:600]}\n")
            api_code = None
            try:
                j = json.loads(raw)
                msg = find_msg(j) or raw
                if isinstance(j, dict) and isinstance(j.get("code"), int):
                    api_code = j["code"]
            except Exception:
                msg = raw
            raw1 = re.sub(r"\s+", " ", raw)
            msg = re.sub(r"\s+", " ", msg)
            item = msg if msg == raw1 else f"{msg} | {raw1}"
            if item.strip():
                out.append({"text": item[:300], "msg": msg, "code": api_code})
        return out


# ---------------------------------------------------------------- page helpers
def find_input(page, sel):
    if sel:
        return page.locator(sel).first
    for s in ("input[placeholder*='code' i]", "input[placeholder*='mã' i]",
              "input[type='text']:visible", "input:not([type]):visible"):
        loc = page.locator(s).first
        if loc.count() and loc.is_visible():
            return loc
    raise RuntimeError("Khong tim thay o nhap code, dung --input '<selector>'")


SUBMIT_RE = re.compile(r"^\s*(xác nhận|đổi( ngay| quà)?|redeem( now)?|submit|confirm|nhận( ngay)?|gửi)\s*$", re.I)
CLOSE_RE = re.compile(r"^\s*(ok|đóng|close|xác nhận|confirm|got it|đã hiểu)\s*$", re.I)


def find_submit(page, sel):
    """Tim nut submit: button that, button[type=submit], roi toi bat ky element nao co text khop."""
    if sel:
        loc = page.locator(sel).first
        return loc if loc.count() else None
    for c in (page.get_by_role("button", name=SUBMIT_RE),
              page.locator("button[type='submit'], input[type='submit']"),
              page.get_by_text(SUBMIT_RE)):
        for i in range(min(c.count(), 6)):
            el = c.nth(i)
            if el.is_visible():
                return el
    return None


def dismiss_popup(page):
    """Dong popup ket qua (neu co) de code tiep theo khong bi chan."""
    try:
        for sel in ("[role='dialog']:visible", "[class*='modal' i]:visible",
                    "[class*='dialog' i]:visible", "[class*='popup' i]:visible"):
            pop = page.locator(sel).first
            if pop.count():
                b = pop.get_by_text(CLOSE_RE).first
                if b.count():
                    b.click(timeout=1500)
                else:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                return
        page.keyboard.press("Escape")
    except Exception:
        pass


def dump_clickables(page, path):
    """Khi khong tim thay nut: ghi cac element bam duoc vao debug.log de chinh --submit."""
    try:
        items = page.evaluate("""() => [...document.querySelectorAll('button,a,[role=button],div,span,input[type=submit]')]
            .filter(e => e.offsetParent && e.children.length < 3 &&
                   (getComputedStyle(e).cursor === 'pointer' || e.tagName === 'BUTTON'))
            .slice(0, 40)
            .map(e => e.tagName + '.' + String(e.className || '').slice(0, 60) + ' | ' + (e.innerText || e.value || '').trim().slice(0, 40))""")
        with Path(path).open("a", encoding="utf-8") as f:
            f.write("CLICKABLES (khong tim thay nut submit):\n  " + "\n  ".join(items) + "\n")
    except Exception:
        pass


def captcha_visible(page):
    return page.locator("iframe[src*='captcha' i], iframe[src*='recaptcha' i], "
                        ".geetest_panel, [class*='captcha' i]:visible").count() > 0


def run_one(page, code, a, sniff, tick):
    sniff.clear()
    inp = find_input(page, a.input)
    inp.click()
    inp.fill("")
    inp.fill(code)
    if inp.input_value() != code:
        return "NO_RESPONSE", "khong dien duoc code vao o nhap (sai selector? dung --input)"
    btn = find_submit(page, a.submit)
    how = "click nut" if btn else "nhan Enter (khong thay nut, xem CLICKABLES trong debug.log)"
    if btn:
        btn.click()
    else:
        if not sniff.dumped:
            dump_clickables(page, sniff.debug)
            sniff.dumped = True
        inp.press("Enter")

    end = time.time() + a.timeout
    while time.time() < end and not sniff.resps:
        page.wait_for_timeout(150)
        tick()
    page.wait_for_timeout(700)

    items = sniff.messages(code)
    msg = " | ".join(i["text"] for i in items)
    api = next((i for i in items if i["code"] is not None), None)
    if not msg:
        try:
            loc = page.locator(a.result).first
            if loc.count():
                msg = loc.inner_text(timeout=1500).strip().replace("\n", " ")
        except Exception:
            pass
    if captcha_visible(page):
        return "CAPTCHA", msg or "captcha hien tren trang"
    if not msg:
        return "NO_RESPONSE", f"da {how} nhung khong thay request/response nao"
    if api:
        if api["code"] == 0:
            return "SUCCESS", msg
        st = classify(api["msg"])  # code != 0 thi khong bao gio la SUCCESS
        return ("UNKNOWN" if st == "SUCCESS" else st), msg
    return classify(msg), msg


# ---------------------------------------------------------------- render
def fmt_dur(s):
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render(st, console):
    root = Layout()
    root.split_column(Layout(name="head", size=5), Layout(name="body"),
                      Layout(name="prog", size=5), Layout(name="foot", size=3))
    root["body"].split_row(Layout(name="stats", ratio=1, minimum_size=30), Layout(name="tbl", ratio=3))

    color = {"RUNNING": "green", "PAUSED": "yellow"}.get(st.status.split()[0], "red")
    badge = Text(f" {st.status} ", style=f"bold black on {color}")
    title = Text("▌ DELTA FORCE · REDEEM ▐", style="bold bright_green")
    sub = Text.assemble(("profile ", "dim"), (st.profile, "cyan"), ("   elapsed ", "dim"),
                        (fmt_dur(time.time() - st.t0), "white"), ("   ", ""), badge)
    root["head"].update(Panel(Align.center(Group(title, sub)), border_style="green", box=box.HEAVY))

    g = Table.grid(padding=(0, 2))
    g.add_column(); g.add_column(justify="right")
    for k in ("SUCCESS", "USED", "INVALID", "EXPIRED", "UNKNOWN", "NO_RESPONSE"):
        g.add_row(Text(f"{ICON[k]} {k}", style=STYLE[k]), Text(str(st.counts[k]), style=STYLE[k]))
    g.add_row("", "")
    avg = sum(st.durations) / len(st.durations) if st.durations else 0
    eta = avg * (st.total - st.done)
    g.add_row(Text("done", style="dim"), f"{st.done}/{st.total}")
    g.add_row(Text("avg/code", style="dim"), f"{avg:.1f}s")
    g.add_row(Text("ETA", style="dim"), fmt_dur(eta) if avg else "-")
    root["stats"].update(Panel(g, title="stats", border_style="cyan"))

    t = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold dim")
    t.add_column("time", width=8, style="dim")
    t.add_column("code", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("message", overflow="ellipsis", no_wrap=True, ratio=1)
    n = max(5, console.height - 18)
    for ts, code, stt, msg in list(st.rows)[-n:]:
        t.add_row(ts, code, Text(f"{ICON[stt]} {stt}", style=STYLE[stt]), msg)
    root["tbl"].update(Panel(t, title="recent", border_style="blue"))

    cd = f"   cooldown {st.countdown:0.0f}s" if st.countdown else ""
    root["prog"].update(Panel(Group(
        ProgressBar(total=max(st.total, 1), completed=st.done, width=None, complete_style="green"),
        Text.assemble(("current ", "dim"), (st.current, "bold white"), (cd, "yellow"))),
        border_style="magenta"))
    root["foot"].update(Panel(Align.center(Text.from_markup(
        "[bold]SPACE[/] pause/resume   [bold]S[/] skip wait   [bold]Q[/] quit")), border_style="dim"))
    return root


# ---------------------------------------------------------------- browser
BROWSERS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/brave-browser", "/usr/bin/google-chrome",
]


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch_browser(a, profile_dir, console):
    """Tu mo browser that (khong co co automation) o che do debug, tra ve CDP url."""
    url = f"http://127.0.0.1:{a.port}"
    if port_open(a.port):
        return url
    exe = a.exe or next((b for b in BROWSERS if os.path.exists(b)), None)
    if not exe:
        console.print("[red]Khong tim thay Brave/Chrome/Edge. Dung --exe <duong dan>[/]")
        sys.exit(1)
    subprocess.Popen([exe, f"--remote-debugging-port={a.port}", f"--user-data-dir={profile_dir}",
                      "--no-first-run", a.url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if port_open(a.port):
            return url
        time.sleep(0.2)
    console.print("[red]Browser khong mo duoc cong debug, thu --port 9333[/]")
    sys.exit(1)


def wait_for_redeem_page(ctx, a, console):
    """Cho toi khi bro login xong va o nhap code hien ra, roi tu chay."""
    host = urlparse(a.url).hostname
    found = None
    try:
        with console.status("[cyan]Dang cho bro login Garena trong browser vua mo... (Ctrl+C de thoat)[/]"):
            while not found:
                for pg in ctx.pages:
                    try:
                        # so host that, khong so chuoi: URL login co redirect_uri chua ten trang redeem
                        if urlparse(pg.url).hostname == host and find_input(pg, a.input).is_visible():
                            found = pg
                            break
                    except Exception:
                        pass
                time.sleep(1)
        console.input("[green]Thay trang nhap code roi. Kiem tra da login, nhan ENTER de bat dau...[/] ")
        return found
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="codes.txt")
    ap.add_argument("--profile", default="main", help="ten profile browser (moi acc 1 profile)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--input", help="CSS selector o nhap code (mac dinh auto-detect)")
    ap.add_argument("--submit", help="CSS selector nut submit (mac dinh auto-detect)")
    ap.add_argument("--result", default=".toast, .modal, .message, [class*=toast], [class*=msg]")
    ap.add_argument("--delay", default="4,8", help="min,max giay nghi sau code THANH CONG")
    ap.add_argument("--fast-delay", default="1,2", help="min,max giay nghi sau code USED/INVALID/EXPIRED (bo qua nhanh)")
    ap.add_argument("--timeout", type=float, default=10)
    ap.add_argument("--reload", action="store_true", help="reload trang sau moi code")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--exe", help="duong dan browser (mac dinh tu tim Brave/Chrome/Edge)")
    ap.add_argument("--port", type=int, default=9222, help="cong debug")
    a = ap.parse_args()

    console = Console()
    dmin, dmax = (float(x) for x in a.delay.split(","))
    fmin, fmax = (float(x) for x in a.fast_delay.split(","))
    codes = list(dict.fromkeys(c.strip() for c in Path(a.codes).read_text().splitlines() if c.strip()))

    out = Path(a.out)
    done_codes = set()
    if out.exists():
        with out.open(encoding="utf-8") as f:
            done_codes = {r[1] for r in csv.reader(f) if len(r) >= 3 and r[2] in FINAL}
    todo = [c for c in codes if c not in done_codes]
    if not todo:
        console.print("[green]Khong con code nao can chay.[/]")
        return

    profile_dir = Path.home() / ".dfredeem" / a.profile
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(launch_browser(a, profile_dir, console))
        ctx = browser.contexts[0]
        sniff = Sniffer("debug.log")
        ctx.on("response", sniff.on_response)
        console.print(f"[bold]{len(todo)}[/] code can chay ({len(done_codes)} da xong, bo qua).")
        page = wait_for_redeem_page(ctx, a, console)

        st = State(len(todo), a.profile)
        st.status = "RUNNING"
        keys = Keys()
        f = out.open("a", newline="", encoding="utf-8")
        w = csv.writer(f)

        with Live(render(st, console), console=console, screen=True, auto_refresh=False) as live:
            def tick():
                while (k := keys.get()) is not None:
                    if k == " ":
                        st.paused = not st.paused
                        st.status = "PAUSED" if st.paused else "RUNNING"
                    elif k == "s":
                        st.skip = True
                    elif k == "q":
                        st.quit = True
                live.update(render(st, console), refresh=True)

            def sleep_ui(sec, label):
                end = time.time() + sec
                st.status, st.skip = label, False
                while time.time() < end and not st.quit and not st.skip:
                    st.countdown = end - time.time()
                    tick()
                    time.sleep(0.1)
                st.countdown, st.skip, st.status = 0, False, "RUNNING"

            def hold():
                while st.paused and not st.quit:
                    st.status = "PAUSED"
                    tick()
                    time.sleep(0.1)

            try:
                i = 0
                while i < len(todo) and not st.quit:
                    code = todo[i]
                    st.current = code
                    hold()
                    if st.quit:
                        break
                    t_start = time.time()
                    try:
                        status, msg = run_one(page, code, a, sniff, tick)
                    except Exception as e:
                        status, msg = "NO_RESPONSE", f"{type(e).__name__}: {str(e)[:120]}"

                    ts = datetime.now().strftime("%H:%M:%S")
                    st.rows.append((ts, code, status, msg))
                    w.writerow([ts, code, status, msg, a.profile])
                    f.flush()

                    if status == "CAPTCHA":
                        st.paused = True
                        st.status = "PAUSED"
                        st.rows.append((ts, "-", "CAPTCHA", "giai tay trong browser roi nhan SPACE"))
                        hold()
                        continue  # thu lai cung code
                    if status == "RATELIMIT":
                        st.strikes += 1
                        if st.strikes >= 5:
                            st.rows.append((ts, "-", "RATELIMIT", "5 lan lien tiep, dung de bao ve acc"))
                            st.quit = True
                            break
                        sleep_ui(min(600, 45 * 2 ** (st.strikes - 1)), "COOLDOWN")
                        continue

                    st.strikes = 0
                    if status in ("NO_RESPONSE", "UNKNOWN"):
                        st.bad += 1
                        if st.bad >= 2:
                            st.rows.append((ts, "-", status, "2 lan lien tiep khong doc duoc ket qua - xem debug.log, SPACE de chay tiep"))
                            st.paused = True
                            st.status = "PAUSED"
                    else:
                        st.bad = 0
                    st.counts[status] += 1
                    st.done += 1
                    st.durations.append(time.time() - t_start)
                    i += 1
                    dismiss_popup(page)
                    if a.reload:
                        page.reload()
                    lo, hi = (fmin, fmax) if status in ("USED", "INVALID", "EXPIRED") else (dmin, dmax)
                    sleep_ui(random.uniform(lo, hi), "RUNNING")
            finally:
                tick()
                keys.stop()
                f.close()

        browser.close()  # chi ngat ket noi, browser van mo de chay tiep lan sau

    c = st.counts
    console.print(f"\n[bold green]Xong.[/] SUCCESS={c['SUCCESS']} USED={c['USED']} "
                  f"INVALID={c['INVALID']} EXPIRED={c['EXPIRED']} UNKNOWN={c['UNKNOWN']} -> {out}")


if __name__ == "__main__":
    main()
