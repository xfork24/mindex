const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(tag) {
    this.tag = tag;
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.attributes = {};
    this.events = {};
    this.paused = true;
    this.readyState = 4;
    this.currentTime = 0;
    this.hidden = false;
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceWith() {}
  cloneNode() {
    return new Element(this.tag);
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  getAttribute(name) {
    return name === "src" ? this.src : this.attributes[name];
  }
  addEventListener(name, fn) {
    this.events[name] = fn;
  }
  load() {
    if (this.deferMetadata) {
      return;
    }
    if (this.onloadedmetadata) {
      this.onloadedmetadata();
    }
  }
  play() {
    this.paused = false;
    return Promise.resolve();
  }
  pause() {
    this.paused = true;
  }
}
const playlist = {
  duration: 2,
  items: [
    { url: "/a", start: 0.5, end: 1.5, offset: 0 },
    { url: "/b", start: 2, end: 3, offset: 1 },
  ],
};
const context = {
  window: {},
  document: { createElement: (tag) => new Element(tag) },
  fetch: async () => ({ ok: true, json: async () => playlist }),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync("frontend/src/shared/shot-player.js", "utf8"), context);
(async () => {
  const Player = context.window.ShotPlayer;
  const first = new Element("video");
  const player = Player.attach(first, "/playlist");
  await player.ready;
  assert.equal(player.current.currentTime, 0.5);
  await player.overlay.onclick();
  assert.equal(player.current.paused, false);
  player.current.onclick();
  await Promise.resolve();
  assert.equal(player.current.paused, true);
  assert.equal(player.videos[1].src, "/b");
  assert.equal(player.videos[1].currentTime, 2);
  await player.seek(1.4);
  assert.equal(player.index, 1);
  assert.equal(player.current.currentTime, 2.4);
  assert.equal(player.time, 1.4);
  await player.seek(0.8, true);
  assert.equal(player.current.paused, false);
  player.current.currentTime = 1.5;
  player.current.events.timeupdate();
  assert.equal(player.index, 1);
  assert.equal(player.active, 1);
  assert.equal(player.current.hidden, false);
  assert.equal(player.current.paused, false);
  await player.seek(0);
  assert.equal(player.index, 0);
  assert.equal(player.time, 0);
  player.videos[0].hidden = true;
  player.active = 1;
  await player.load("/playlist");
  assert.equal(player.active, 0);
  assert.equal(player.current.hidden, false);
  assert.equal(player.toggle.type, "button");
  assert.equal(Player.playlistURL("/api/media/abc"), "/api/playlists/abc");
  const delayedVideo = new Element("video");
  delayedVideo.readyState = 0;
  delayedVideo.deferMetadata = true;
  const delayed = new Player(delayedVideo);
  delayed.items = playlist.items;
  delayed.bar.max = playlist.duration;
  const seeking = delayed.seek(0.4, true);
  assert.equal(delayedVideo.paused, true);
  delayedVideo.readyState = 4;
  delayedVideo.onloadedmetadata();
  await seeking;
  assert.equal(delayedVideo.currentTime, 0.9);
  assert.equal(delayedVideo.paused, false);
  console.log("shot player timeline, seek, preload, alternating playback and replay passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
