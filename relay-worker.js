// Dragon Alley relay — Cloudflare Worker + Durable Object (v5)
// ----------------------------------------------------------------------
// One Durable Object per room code pairs ONE host WebSocket with ONE
// guest WebSocket and forwards every game message between them. No game
// logic lives here: hello/start, lockstep inputs, ping/pong and the v5
// resume handshake all pass through the pipe verbatim.
//
// Game side connects to:
//   wss://<this-worker>/ws?room=ABC123&role=host&sid=<session-id>
//   wss://<this-worker>/ws?room=ABC123&role=guest&sid=<session-id>
// A reconnecting client adds &resume=1 and reuses the SAME sid.
//
// v5: when a socket drops, its slot is NOT released immediately. The
// {sid, grace-until} pair is persisted in Durable Object storage, so the
// same device can reclaim its slot for GRACE_MS — even if this DO instance
// was evicted while both phones were offline. A different sid is refused
// during the grace window, which stops a stale client (or a stranger) from
// stealing half of a live session.
//
// Control messages to a client: {"type":"__relay","event":...}
//   paired | guest-joined | peer-resumed | peer-left | waiting-host |
//   no-host | room-full | room-taken

const GRACE_MS = 60000;

export class Room {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.host = null;
    this.guest = null;
    this.meta = { host: { sid: "", until: 0 }, guest: { sid: "", until: 0 } };
    // Storage survives DO eviction — essential for two phones that both go
    // offline at the same moment (lift, tunnel, plane mode).
    this._load = state.storage.get("da-session")
      .then((v) => { if (v && v.host && v.guest) this.meta = v; })
      .catch(() => {});
  }

  ctl(ws, event) {
    try { if (ws) ws.send(JSON.stringify({ type: "__relay", event: event })); } catch (e) {}
  }
  alive(ws) { return !!ws; }
  save() {
    try { return this.state.storage.put("da-session", this.meta).catch(() => {}); }
    catch (e) { return Promise.resolve(); }
  }
  expire() {
    const now = Date.now();
    for (const r of ["host", "guest"]) {
      if (this.meta[r].until && this.meta[r].until < now) this.meta[r] = { sid: "", until: 0 };
    }
  }
  reject(server, client, event, reason) {
    this.ctl(server, event);
    setTimeout(() => { try { server.close(1008, reason); } catch (e) {} }, 220);
    return new Response(null, { status: 101, webSocket: client });
  }

  async fetch(request) {
    await this._load;
    const url = new URL(request.url);
    const role = url.searchParams.get("role");
    const sid = url.searchParams.get("sid") || "";
    const resume = url.searchParams.get("resume") === "1";

    if (request.headers.get("Upgrade") !== "websocket")
      return new Response("expected a websocket upgrade", { status: 426 });

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.accept();

    if (role !== "host" && role !== "guest") {
      try { server.close(1008, "bad role"); } catch (e) {}
      return new Response(null, { status: 101, webSocket: client });
    }

    this.expire();
    const now = Date.now();
    const slot = this.meta[role];
    const occupied = this.alive(this[role]);
    const grace = slot.until > now;
    const reclaim = !!(sid && slot.sid && slot.sid === sid);
    const otherRole = role === "host" ? "guest" : "host";
    const busyEvent = role === "host" ? "room-taken" : "room-full";

    // Busy (live socket) or reserved (grace for a different sid).
    if ((occupied || (!occupied && grace)) && !reclaim)
      return this.reject(server, client, busyEvent, "slot busy");

    // A fresh guest with no host at all gets the classic "no host" answer.
    // A RETURNING guest (&resume=1) is allowed to wait for the host to come
    // back, so both devices can reconnect in either order.
    if (role === "guest" && !this.alive(this.host) &&
        !(this.meta.host.until > now) && !resume)
      return this.reject(server, client, "no-host", "no host");

    // Close a stale live socket when the same sid reclaims its slot.
    if (this[role] && this[role] !== server) {
      try { this[role].close(1000, "replaced by resume"); } catch (e) {}
    }
    const wasResuming = reclaim && grace;
    this[role] = server;
    this.meta[role] = { sid: sid || slot.sid || "", until: 0 };
    await this.save();

    const peer = this[otherRole];
    if (this.alive(peer)) {
      if (wasResuming) {
        this.ctl(server, "peer-resumed");
        this.ctl(peer, "peer-resumed");
      } else if (role === "guest") {
        this.ctl(server, "paired");
        this.ctl(peer, "guest-joined");
      } else {
        this.ctl(server, "guest-joined");
        this.ctl(peer, "paired");
      }
    } else {
      this.ctl(server, role === "guest" ? "waiting-host" : (wasResuming ? "peer-resumed" : "paired"));
    }

    server.addEventListener("message", (ev) => {
      let data = ev.data;
      // An explicit "bye" is a clean quit: release this slot immediately so
      // a brand-new session can reuse the room code without waiting.
      try {
        const m = JSON.parse(data);
        if (m && m.type === "bye") {
          server._bye = true;                 // do NOT re-reserve on close
          this.meta[role] = { sid: "", until: 0 };
          this.save();
        }
      } catch (e) {}
      const to = this[otherRole];
      if (this.alive(to)) { try { to.send(data); } catch (e) {} }
    });

    const gone = () => {
      if (this[role] !== server) return;   // slot already reclaimed by a resume
      this[role] = null;
      // A clean quit releases the room code immediately; a network drop
      // reserves it so the same device can reclaim its old slot.
      this.meta[role] = server._bye
        ? { sid: "", until: 0 }
        : { sid: this.meta[role].sid || sid, until: Date.now() + GRACE_MS };
      this.save();
      const p = this[otherRole];
      if (this.alive(p)) this.ctl(p, "peer-left");
    };
    server.addEventListener("close", gone);
    server.addEventListener("error", gone);

    return new Response(null, { status: 101, webSocket: client });
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/ws") {
      const room = (url.searchParams.get("room") || "").toUpperCase();
      if (!/^[A-Z0-9]{6}$/.test(room)) return new Response("bad room code", { status: 400 });
      const id = env.ROOMS.idFromName(room);
      const stub = env.ROOMS.get(id);
      return stub.fetch(request);
    }
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(
        "Dragon Alley relay v5 is running.\n" +
        "Game connects to /ws?room=CODE&role=host|guest[&sid=ID][&resume=1] over a websocket.\n" +
        "Disconnected clients keep their slot for 60s and can reclaim it with the same sid.\n",
        { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }
    return new Response("not found", { status: 404 });
  },
};
