/* Logical clip timeline backed by two preloaded shot decoders. */
window.ShotPlayer = class ShotPlayer {
  constructor(video) {
    this.first = video;this.videos = [video, video.cloneNode(false)];this.active = 0;this.index = 0;this.generation = 0;
    const pause = this.videos.map(v => v.pause.bind(v));video.pause = () => pause.forEach(fn => fn());
    this.container = document.createElement('div');this.container.className = 'shot-player';this.container.style.position = 'relative';
    video.replaceWith(this.container);
    this.media = document.createElement('div');this.media.className = 'shot-player-media';this.container.append(this.media);this.media.append(...this.videos);
    this.overlay = document.createElement('button');this.overlay.type = 'button';this.overlay.className = 'shot-play-overlay';this.overlay.textContent = '▶';this.label(this.overlay, 'ui.player_play', 'Play video');
    this.overlay.onclick = () => this.togglePlayback();this.media.append(this.overlay);
    this.videos[1].removeAttribute('id');this.videos.forEach(v => {v.style.width = '100%';v.style.maxHeight = '75vh';});
    this.bar = document.createElement('input');this.bar.type = 'range';this.bar.min = 0;this.bar.step = .01;this.bar.value = 0;
    const controls = document.createElement('div');controls.className = 'shot-player-controls';
    this.toggle = document.createElement('button');this.toggle.type = 'button';this.label(this.toggle, 'ui.player_toggle', 'Play / pause');this.toggle.textContent = '▶ / Ⅱ';
    this.toggle.onclick = () => this.togglePlayback();
    const replay = document.createElement('button');replay.type = 'button';this.label(replay, 'ui.player_replay', 'Replay');replay.textContent = '↻';replay.onclick = () => this.seek(0, true);
    const speed = document.createElement('select');this.label(speed, 'ui.player_speed', 'Playback speed');
    for(const rate of [.5, 1, 1.5, 2]) {const option = document.createElement('option');option.value = rate;option.textContent = `${rate}×`;option.selected = rate === 1;speed.append(option);}
    speed.onchange = () => this.videos.forEach(v => v.playbackRate = Number(speed.value));
    const full = document.createElement('button');full.type = 'button';this.label(full, 'ui.player_fullscreen', 'Fullscreen');full.textContent = '⛶';full.onclick = () => this.container.requestFullscreen();
    controls.append(this.toggle, replay, speed, full, this.bar);this.container.append(controls);
    this.label(this.bar, 'ui.player_position', 'Clip position');this.bar.oninput = () => this.seek(Number(this.bar.value), !this.current.paused);
    this.videos.forEach((v, n) => {v.controls = false;v.preload = 'auto';v.hidden = n !== 0;
      v.onclick = () => {if(n === this.active) this.togglePlayback();};
      v.addEventListener('play', () => {if(n === this.active) this.overlay.hidden = true;});
      v.addEventListener('pause', () => {if(n === this.active) this.overlay.hidden = false;});
      v.addEventListener('timeupdate', () => {if(n !== this.active || !this.items) return;const item = this.items[this.index];this.bar.value = item.offset + v.currentTime - item.start;
        if(v.currentTime >= item.end - .015) this.next();});
      v.addEventListener('ended', () => {if(n === this.active) this.next();});
      v.addEventListener('error', () => this.fail(Error('镜头解码失败')));
    });
  }
  label(node, key, fallback) {
    const i18n = window.MindexI18n;
    if (i18n) i18n.setAttr(node, 'aria-label', i18n.msg(key));
    else node.setAttribute('aria-label', fallback);
  }
  async togglePlayback() {
    try {await this.ready;const v = this.current;if(v.paused) await v.play();else v.pause();} catch(error) {this.fail(error);}
  }
  get current() {return this.videos[this.active];}
  get time() {const item = this.items?.[this.index];return item ? item.offset + this.current.currentTime - item.start : 0;}
  fail(error) {this.container.dataset.error = error.message;if (window.MindexI18n) window.MindexI18n.setAttr(this.container, 'title', window.MindexI18n.system(error.message));else this.container.title = error.message;this.current.pause();}
  async load(url, position = 0) {
    this.videos.forEach(v => {v.pause();v.removeAttribute('src');v.load();});
    this.overlay.hidden = false;this.active = 0;this.index = 0;this.switching = false;this.items = null;delete this.container.dataset.error;if (window.MindexI18n) window.MindexI18n.setAttr(this.container, 'title', '');else this.container.title = '';
    this.videos.forEach((v, n) => {v.hidden = n !== 0;});
    const generation = ++this.generation;
    const response = await fetch(url);if(!response.ok) throw Error(await response.text());
    const playlist = await response.json();if(generation !== this.generation) return;this.items = playlist.items;this.bar.max = playlist.duration;await this.seek(position, false);
  }
  prepare(video, item, position) {
    if(!item) return Promise.resolve();
    return new Promise(resolve => {
      const set = () => {video.currentTime = position;resolve();};
      if(video.getAttribute('src') !== item.url || !video.readyState) {video.onloadedmetadata = set;video.src = item.url;video.load();} else set();
    });
  }
  async seek(position, playing = false) {
    if(!this.items?.length) return;
    position = Math.max(0, Math.min(Number(this.bar.max) - .000001, position));
    const index = this.items.findIndex(item => position >= item.offset && position < item.offset + item.end - item.start);
    if(index < 0) return;
    this.current.pause();this.index = index;
    const current = this.current, generation = this.generation;
    const ready = this.prepare(current, this.items[index], this.items[index].start + position - this.items[index].offset);
    this.prepare(this.videos[1-this.active], this.items[index+1], this.items[index+1]?.start);
    this.bar.value = position;await ready;
    if(playing && generation === this.generation && current === this.current && index === this.index) current.play().catch(e => this.fail(e));
  }
  next() {
    if(this.switching) return;
    if(this.index + 1 >= this.items.length) {this.current.pause();return;}
    this.switching = true;this.current.pause();this.current.hidden = true;this.active = 1-this.active;this.index++;
    this.current.hidden = false;this.current.play().catch(e => this.fail(e));
    this.prepare(this.videos[1-this.active], this.items[this.index+1], this.items[this.index+1]?.start);
    this.switching = false;
  }
  static playlistURL(url) {
    let match = url?.match(/^\/api\/media\/([^/?]+)/);
    if(match) return `/api/playlists/${match[1]}`;
    match = url?.match(/^\/api\/admin\/(segments|sources)\/([^/?]+)\/media/);
    return match ? `/api/admin/${match[1]}/${match[2]}/playlist` : null;
  }
  static attach(video, url) {const player = video.shotPlayer || (video.shotPlayer = new ShotPlayer(video));player.ready = player.load(url);player.ready.catch(e => player.fail(e));return player;}
};
