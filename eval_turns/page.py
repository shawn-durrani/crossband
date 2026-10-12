"""The real voice page in a headless Chromium, with a made-up microphone.

Everything that decides a turn runs in the page: the voice detector, the
1.5 second pause, the pause and cut of a reply when someone talks over it,
the word rules and the call to the judge. So the rig drives the page
itself, as you would, and times what it does.

Before the app's own code loads, a small script (INIT_JS) puts three things
in the page:

- a microphone: getUserMedia hands the app a stream the rig plays clips
  into, under a faint hiss so the detector has a room to measure
- a record of the app's own debug lines, which carry the page's clock and
  never any words (voice.js _vlog)
- a record of when the turn is sent, when a round is stopped, when the
  judge is asked, and when any audio element starts or stops playing

All of it is on the page's clock (performance.now), so a clip's start and
every decision line up to a few milliseconds.
"""

import asyncio
import base64

INIT_JS = r"""
(() => {
  const HISS = 0.0015;
  const rig = window.__rig = {log: [], fetch: [], media: [], ctx: null, mixer: null};
  const now = () => performance.now();

  const debug = console.debug.bind(console);
  console.debug = function (...args) {
    try {
      const m = typeof args[0] === 'string' && args[0].match(/^\[(voice|round)\] (?:([\d.]+)ms )?(\S+)/);
      if (m) {
        let data = null;
        try { data = JSON.parse(JSON.stringify(args[1] ?? null)) } catch (e) { data = null }
        rig.log.push({t: m[2] ? parseFloat(m[2]) : now(), src: m[1], tag: m[3], data});
      }
    } catch (e) {}
    return debug(...args);
  };

  const WATCH = /\/api\/chats\/\d+\/(send|round\/abort|stt)$|\/api\/voice\/judge$/;
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (!WATCH.test(path)) return origFetch(input, init);
    const rec = {t: now(), path, method: (init && init.method) || 'GET'};
    rig.fetch.push(rec);
    return origFetch(input, init).then(
      r => { rec.status = r.status; rec.end = now(); return r },
      e => { rec.error = String(e); rec.end = now(); throw e });
  };

  const seen = new WeakSet();
  const watch = (el) => {
    if (seen.has(el)) return;
    seen.add(el);
    for (const ev of ['playing', 'pause', 'ended']) {
      el.addEventListener(ev, () => rig.media.push(
        {t: now(), ev, dur: isFinite(el.duration) ? el.duration : -1}));
    }
  };
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () { watch(this); return play.apply(this, arguments) };

  // What the app's own level meter hears, the loudest in each 100 ms, so
  // a run can show whether a clip reached the detector at all.
  rig.levels = [];
  const getTD = AnalyserNode.prototype.getFloatTimeDomainData;
  AnalyserNode.prototype.getFloatTimeDomainData = function (buf) {
    getTD.call(this, buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length), t = now(), slot = Math.floor(t / 100);
    const last = rig.levels[rig.levels.length - 1];
    if (last && last.slot === slot) last.rms = Math.max(last.rms, rms);
    else rig.levels.push({slot, t: slot * 100, rms});
    if (rig.levels.length > 6000) rig.levels.shift();
  };

  const ensure = () => {
    if (rig.ctx) return;
    const ctx = rig.ctx = new AudioContext();
    rig.mixer = ctx.createGain();
    const n = ctx.sampleRate * 2;
    const buf = ctx.createBuffer(1, n, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * HISS;
    const hiss = ctx.createBufferSource();
    hiss.buffer = buf; hiss.loop = true; hiss.connect(rig.mixer); hiss.start();
  };
  navigator.mediaDevices.getUserMedia = async function (constraints) {
    if (!constraints || !constraints.audio) {
      throw new DOMException('the rig has a microphone only', 'NotFoundError');
    }
    ensure();
    if (rig.ctx.state !== 'running') await rig.ctx.resume();
    const dest = rig.ctx.createMediaStreamDestination();
    rig.mixer.connect(dest);
    return dest.stream;
  };

  rig.play = async (b64, rate) => {
    ensure();
    if (rig.ctx.state !== 'running') await rig.ctx.resume();
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const pcm = new Int16Array(bytes.buffer);
    const buf = rig.ctx.createBuffer(1, pcm.length, rate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) d[i] = pcm[i] / 32768;
    const src = rig.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(rig.mixer);
    const lead = 0.05;
    const at = now() + lead * 1000;
    src.start(rig.ctx.currentTime + lead);
    return at;
  };
})();
"""

TALK = "talk"       # the header's own button, at desktop width
END_LABEL = "End voice session and stop the microphone"
QUIET_S = 2.0


class PageError(RuntimeError):
    pass


def silence_js(seconds: float) -> str:
    """The app's own pause setting, as the settings drawer stores it."""
    return (f"localStorage.setItem('voice_silence_secs', '{seconds:g}');"
            "localStorage.setItem('voice_silence_picked', '1');")


class Page:
    def __init__(self, base: str, silence_s: float = 0.0):
        self.base = base
        self.silence_s = silence_s
        self._pw = self._browser = self.page = None

    async def open(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise PageError("Playwright isn't installed; the app's web viewing "
                            "uses it too (`.venv/bin/playwright install chromium`)") from e
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as e:
            await self._pw.stop()
            raise PageError("Chromium didn't start; run `.venv/bin/playwright "
                            "install chromium` once") from e
        ctx = await self._browser.new_context(viewport={"width": 1280, "height": 900})
        await ctx.add_init_script(INIT_JS)
        if self.silence_s:
            await ctx.add_init_script(silence_js(self.silence_s))
        self.page = await ctx.new_page()
        await self.page.goto(self.base)
        return self

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def open_chat(self, title: str):
        await self.page.get_by_text(title, exact=True).first.click()
        await self._talk().wait_for(timeout=15000)

    def _talk(self):
        return self.page.get_by_role("button", name=TALK, exact=True)

    async def start_voice(self):
        await self._talk().click()
        await self.wait_state("listening", timeout=30)

    async def stop_voice(self):
        # The dock's End button is laid out for the width it's on, and the
        # one in the DOM may be the hidden twin, so it's pressed directly.
        await self.page.evaluate(
            "(label) => document.querySelector(`[aria-label=\"${label}\"]`).click()",
            END_LABEL)
        await self.wait_state("off", timeout=15)

    async def now(self) -> float:
        return await self.page.evaluate("performance.now()")

    async def play(self, pcm: bytes, rate: int) -> float:
        """Plays a clip into the microphone. Returns when it starts, on the
        page's clock."""
        b64 = base64.b64encode(pcm).decode()
        return await self.page.evaluate("([b, r]) => window.__rig.play(b, r)", [b64, rate])

    async def events(self) -> dict:
        return await self.page.evaluate(
            "() => ({log: window.__rig.log, fetch: window.__rig.fetch, "
            "media: window.__rig.media, levels: window.__rig.levels})")

    async def state(self) -> tuple:
        """The newest state and when it was entered."""
        return tuple(await self.page.evaluate(
            "() => { const s = window.__rig.log.filter(e => e.tag === 'state');"
            " const l = s[s.length - 1]; return l ? [l.data && l.data.to, l.t] : ['', 0] }"))

    async def wait_state(self, want: str, timeout: float):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if (await self.state())[0] == want:
                return
            await asyncio.sleep(0.1)
        raise PageError(f"the page didn't reach {want!r} in {timeout:.0f}s")

    async def wait_quiet(self, timeout: float = 45.0) -> bool:
        """Listening, and for QUIET_S at least, with no audio playing."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            state, since = await self.state()
            now = await self.now()
            media = (await self.events())["media"]
            playing = bool(media) and media[-1]["ev"] == "playing"
            if state == "listening" and not playing and now - since >= QUIET_S * 1000:
                return True
            await asyncio.sleep(0.2)
        return False

    async def wait_playing(self, after: float, timeout: float = 30.0):
        """When audio next starts playing after `after`, on the page's
        clock, or None."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for e in (await self.events())["media"]:
                if e["ev"] == "playing" and e["t"] > after:
                    return e["t"]
            await asyncio.sleep(0.05)
        return None

    async def sleep_until(self, t: float):
        gap = (t - await self.now()) / 1000
        if gap > 0:
            await asyncio.sleep(gap)
