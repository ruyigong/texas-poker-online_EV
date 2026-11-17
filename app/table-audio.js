const tableAudio = (() => {
  const controls = document.getElementById("tableAudio");
  const status = document.getElementById("trackStatus");
  const enable = document.getElementById("enableTableAudio");
  const fileInput = document.getElementById("trackFile");
  const play = document.getElementById("requestTrack");
  const uploadStatus = document.getElementById("musicUploadStatus");
  const videoForm = document.getElementById("requestVideo");
  const videoUrl = document.getElementById("videoUrl");
  const shareVideo = document.getElementById("shareVideo");
  const videoRequestStatus = document.getElementById("videoRequestStatus");
  const videoSection = document.getElementById("roomVideo");
  const videoTitle = document.getElementById("roomVideoTitle");
  const videoFrame = document.getElementById("roomVideoFrame");
  const videoStatus = document.getElementById("roomVideoStatus");
  const openVideoOnYouTube = document.getElementById("openVideoOnYouTube");
  const watchVideo = document.getElementById("watchRoomVideo");
  const closeVideo = document.getElementById("closeRoomVideo");
  const effects = {
    bet: new Audio("/audios/betting.mp3"),
    buyTime: new Audio("/audios/buy-time.wav"),
    check: new Audio("/audios/check.mp3"),
    fold: new Audio("/audios/fold.mp3"),
    win: new Audio("/audios/winning.mp3"),
  };
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  let context;
  let musicGain;
  let effectsGain;
  let session;
  let track;
  let uploading = false;
  let sharingVideo = false;
  let audioEnabled = false;
  let videoHidden = false;
  let embedded;
  let youtubeAPI;
  let sharedVideoId;

  function bindVolume(id, applyVolume) {
    const input = document.getElementById(id);
    const output = document.getElementById(id + "Value");
    const key = "poker:" + id;
    try {
      const saved = localStorage.getItem(key);
      if (saved !== null && /^\d{1,3}$/.test(saved)) input.value = saved;
    } catch {}
    input.addEventListener("input", () => {
      output.value = input.value + "%";
      try { localStorage.setItem(key, input.value); } catch {}
      applyVolume();
    });
    output.value = input.value + "%";
    return input;
  }

  const musicVolume = bindVolume("musicVolume", () => applyMusicVolume(true));
  const effectsVolume = bindVolume("effectsVolume", applyEffectsVolume);

  function applyMusicVolume(forceUnmute = false) {
    const musicLevel = Number(musicVolume.value);
    if (musicGain) musicGain.gain.value = musicLevel / 100;
    if (embedded?.ready) {
      embedded.player.setVolume(musicLevel);
      if (musicLevel === 0) embedded.player.mute();
      else if (forceUnmute || embedded.musicMuted) embedded.player.unMute();
      embedded.musicMuted = musicLevel === 0;
    }
  }

  function applyEffectsVolume() {
    const level = Number(effectsVolume.value) / 100;
    if (effectsGain) effectsGain.gain.value = level;
    for (const [kind, audio] of Object.entries(effects)) {
      const baseVolume = kind === "win" ? 0.4 : 1;
      audio.volume = baseVolume * (effectsGain ? 1 : level);
      audio.muted = level === 0;
    }
  }

  function getContext() {
    if (!context && AudioContextClass) {
      context = new AudioContextClass();
      musicGain = context.createGain();
      effectsGain = context.createGain();
      musicGain.connect(context.destination);
      effectsGain.connect(context.destination);
      for (const [kind, audio] of Object.entries(effects)) {
        const source = context.createMediaElementSource(audio);
        if (kind === "win") {
          const quieter = context.createGain();
          quieter.gain.value = 0.4;
          source.connect(quieter);
          quieter.connect(effectsGain);
        } else {
          source.connect(effectsGain);
        }
      }
      context.addEventListener("statechange", () => {
        if (context.state === "running") startTrack();
        else stopSource();
        refreshControls();
      });
      applyMusicVolume();
      applyEffectsVolume();
    }
    return context;
  }

  function unlockAudio() {
    if (!session) return;
    audioEnabled = true;
    if (track?.video && !embedded && !videoHidden) openVideo();
    try {
      const audio = getContext();
      if (audio && audio.state !== "running") audio.resume().catch(refreshControls);
    } catch {
      status.textContent = "Audio is unavailable in this browser.";
    }
  }

  function refreshControls() {
    fileInput.disabled = !session || uploading || sharingVideo || !AudioContextClass;
    shareVideo.disabled = !session || uploading || sharingVideo;
    play.disabled = fileInput.disabled || !fileInput.files.length;
    enable.hidden = !session || (audioEnabled && (!AudioContextClass || context?.state === "running"));
  }

  function stopSource() {
    if (!track?.source) return;
    track.source.onended = null;
    track.source.stop();
    track.source.disconnect();
    track.source = null;
  }

  function setTrack(next) {
    stopSource();
    removeVideo();
    track?.request?.abort();
    if (track) window.clearTimeout(track.expiration);
    track = next;
    status.textContent = next
      ? next.title + " · " + next.requestedBy
      : "Nothing playing";
    videoSection.hidden = !next?.video;
    videoTitle.textContent = next?.video ? status.textContent : "";
    if (next?.video && next.id === sharedVideoId) {
      sharedVideoId = null;
      audioEnabled = true;
      videoHidden = false;
      videoSection.scrollIntoView({block: "nearest"});
    }
    if (next?.video && audioEnabled && !videoHidden) openVideo();
  }

  function elapsed(current) {
    return current.elapsed + (performance.now() - current.receivedAt) / 1000;
  }

  function startTrack() {
    const current = track;
    if (!current || current.video || current.source || current.failed || context?.state !== "running") return;
    if (!current.buffer) {
      if (!current.request) void loadTrack(current);
      return;
    }
    const offset = elapsed(current);
    if (offset >= current.buffer.duration) {
      setTrack(null);
      return;
    }
    const source = context.createBufferSource();
    source.buffer = current.buffer;
    source.connect(musicGain);
    source.onended = () => {
      if (track === current && track.source === source) {
        track.source = null;
        source.disconnect();
        setTrack(null);
      }
    };
    current.source = source;
    source.start(context.currentTime + Math.max(0, -offset), Math.max(0, offset));
  }

  function musicUrl() {
    return "/room/" + encodeURIComponent(session.room) + "/music";
  }

  async function loadTrack(current) {
    const controller = new AbortController();
    current.request = controller;
    try {
      const response = await fetch(musicUrl() + "/" + current.id, {
        headers: {"X-Player-Id": session.playerId},
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Unavailable music");
      const buffer = await context.decodeAudioData(await response.arrayBuffer());
      if (track !== current) return;
      current.buffer = buffer;
      startTrack();
    } catch {
      if (track !== current) return;
      current.failed = true;
      status.textContent = "Couldn't play " + current.title + ".";
    }
  }

  function update(room, playerId, music) {
    if (room !== session?.room || playerId !== session?.playerId) {
      reset();
      session = {room, playerId};
      refreshControls();
    }
    if ((music?.id ?? null) === (track?.id ?? null)) {
      if (track?.video) {
        const indexChanged = track.video.index !== music.video.index;
        track.video = music.video;
        track.elapsed = music.elapsed;
        track.receivedAt = performance.now();
        if (indexChanged && embedded?.playlistReady && embedded.player.getPlaylistIndex() !== music.video.index) {
          embedded.autoStart = true;
          embedded.startWhenVisible();
        }
      }
      return;
    }
    if (!music) {
      setTrack(null);
      return;
    }
    const current = {...music, receivedAt: performance.now()};
    setTrack(current);
    current.expiration = window.setTimeout(() => {
      if (track === current) setTrack(null);
    }, Math.max(0, (music.duration - music.elapsed) * 1000));
    startTrack();
  }

  function reset() {
    if (!session) return;
    session = null;
    uploading = false;
    sharingVideo = false;
    videoHidden = false;
    sharedVideoId = null;
    videoUrl.value = "";
    videoRequestStatus.hidden = true;
    fileInput.value = "";
    controls.open = false;
    uploadStatus.hidden = true;
    setTrack(null);
    refreshControls();
  }

  function playEffect(kind) {
    const audio = effects[kind];
    if (!audio || Number(effectsVolume.value) === 0) return;
    audio.currentTime = 0;
    audio.play().catch(() => {});
  }

  function showVideoStatus(message, youtubeId) {
    videoStatus.textContent = message;
    videoStatus.hidden = !message;
    openVideoOnYouTube.hidden = !youtubeId;
    if (youtubeId) openVideoOnYouTube.href = "https://www.youtube.com/watch?v=" + encodeURIComponent(youtubeId);
  }

  function removeVideo() {
    const previous = embedded;
    embedded = null;
    previous?.observer?.disconnect();
    if (previous) window.clearTimeout(previous.errorRecovery);
    previous?.player?.destroy();
    videoFrame.replaceChildren();
    closeVideo.hidden = true;
    watchVideo.textContent = "Watch synced video";
    showVideoStatus("");
  }

  function loadYouTubeAPI() {
    if (window.YT?.Player) return Promise.resolve();
    if (!youtubeAPI) {
      youtubeAPI = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        const fail = () => {
          window.clearTimeout(timeout);
          script.remove();
          reject(new Error("YouTube player unavailable"));
        };
        const timeout = window.setTimeout(fail, 10000);
        window.onYouTubeIframeAPIReady = () => {
          window.clearTimeout(timeout);
          resolve();
        };
        script.src = "https://www.youtube.com/iframe_api";
        script.onerror = fail;
        document.head.append(script);
      }).catch(error => {
        youtubeAPI = null;
        throw error;
      });
    }
    return youtubeAPI;
  }

  function playYouTube(video) {
    if (!video.ready || !video.visible || document.hidden) {
      showVideoStatus("Bring the player into view to play.");
      return;
    }
    const time = Math.max(0, elapsed(video.track));
    const {id, playlist, index} = video.track.video;
    const player = video.player;
    applyMusicVolume();
    if (playlist) {
      if (!video.playlistReady) return;
      const items = player.getPlaylist();
      const skipTo = video.skipTo;
      video.skipTo = null;
      const targetIndex = skipTo ?? index ?? Math.max(0, items.indexOf(id));
      const startSeconds = skipTo == null ? time : 0;
      const isCurrent = video.started && player.getPlaylistIndex() === targetIndex;
      if (isCurrent) {
        player.seekTo(time, true);
        player.playVideo();
      } else {
        player.loadPlaylist({list: playlist, listType: "playlist", index: targetIndex, startSeconds});
      }
      video.started = true;
      showVideoStatus("");
      return;
    }
    const duration = player.getDuration();
    if (duration > 0 && time >= duration) {
      player.pauseVideo();
      showVideoStatus("This room video has ended.");
      return;
    }
    showVideoStatus("");
    if (video.started) {
      player.seekTo(time, true);
      player.playVideo();
    } else {
      video.started = true;
      player.loadVideoById({videoId: id, startSeconds: time});
    }
  }

  async function reportPlaylistPosition(video) {
    const previous = video.track.video.index ?? -1;
    const index = video.player.getPlaylistIndex();
    if (!session || !Number.isInteger(index) || index <= previous || video.reportedIndex === index) return;
    video.reportedIndex = index;
    try {
      await fetch(musicUrl() + "/video/advance", {
        method: "POST",
        headers: {
          "X-Player-Id": session.playerId,
          "X-Track-Id": video.track.id,
          "X-From-Index": String(previous),
          "X-To-Index": String(index),
          "X-Track-Position": String(Math.min(30, Math.max(0, video.player.getCurrentTime()))),
        },
      });
    } catch {
      video.reportedIndex = null;
    }
  }

  function openVideo() {
    removeVideo();
    const current = track;
    if (!current?.video) return;
    const {provider, id, part, playlist} = current.video;
    const start = Math.max(0, Math.floor(elapsed(current)));
    const iframe = document.createElement("iframe");
    iframe.title = provider === "youtube" ? "YouTube room video" : "Bilibili room video";
    iframe.allow = "autoplay; encrypted-media; fullscreen";
    iframe.allowFullscreen = true;
    iframe.referrerPolicy = "strict-origin-when-cross-origin";
    const url = provider === "youtube"
      ? new URL("https://www.youtube.com/embed/" + (playlist ? "" : encodeURIComponent(id)))
      : new URL("https://player.bilibili.com/player.html");
    if (provider === "youtube") {
      url.search = new URLSearchParams({enablejsapi: "1", origin: location.origin, playsinline: "1", start});
      if (playlist) {
        url.searchParams.set("listType", "playlist");
        url.searchParams.set("list", playlist);
      }
    } else {
      url.search = new URLSearchParams({bvid: id, p: part, t: start, autoplay: "1", muted: musicVolume.value === "0" ? "1" : "0"});
    }
    const video = {track: current, visible: false, ready: false, autoStart: true};
    embedded = video;
    iframe.src = url.href;
    videoFrame.append(iframe);
    closeVideo.hidden = false;
    watchVideo.textContent = "Sync to room";
    if (provider === "bilibili") {
      showVideoStatus("Adjust Bilibili volume inside the player.");
      return;
    }

    function recoverPlaylist(errorCode, retries = 8) {
      if (embedded !== video) return;
      const player = video.player;
      const items = player.getPlaylist?.() || [];
      const failed = player.getPlaylistIndex?.() ?? -1;
      if ((!items.length || failed < 0) && retries > 0) {
        video.errorRecovery = window.setTimeout(() => recoverPlaylist(errorCode, retries - 1), 250);
        return;
      }
      const requested = current.video.index ?? Math.max(0, items.indexOf(id));
      const next = !video.started && failed !== requested ? requested : failed + 1;
      if (failed >= 0 && next < items.length && video.skippedIndex !== failed) {
        video.skippedIndex = failed;
        video.playlistReady = true;
        video.skipTo = next;
        video.autoStart = true;
        video.startWhenVisible();
        return;
      }
      showYouTubeError(errorCode);
    }
    function showYouTubeError(code) {
      const message = [101, 150].includes(code)
        ? "The uploader disabled embedded playback. Share a different YouTube link for room playback."
        : "This YouTube video cannot play in the embedded player.";
      showVideoStatus(message, id || undefined);
    }
    video.startWhenVisible = () => {
      if (embedded !== video || !video.autoStart || !video.ready || !video.visible || document.hidden) return;
      if (playlist && !video.playlistReady) return;
      video.autoStart = false;
      playYouTube(video);
    };
    video.pauseForVisibility = () => {
      if (!video.ready) return;
      if ([1, 3].includes(video.player.getPlayerState())) video.autoStart = true;
      video.player.pauseVideo();
    };
    video.observer = new IntersectionObserver(([entry]) => {
      if (embedded !== video) return;
      video.visible = entry.intersectionRatio > 0.5;
      if (!video.visible) video.pauseForVisibility();
      video.startWhenVisible();
    }, {threshold: [0, 0.51]});
    video.observer.observe(iframe);
    loadYouTubeAPI().then(() => {
      if (embedded !== video) return;
      video.player = new window.YT.Player(iframe, {
        events: {
          onReady(event) {
            if (embedded !== video) return;
            video.ready = true;
            video.player = event.target;
            applyMusicVolume();
            if (playlist) {
              video.player.cuePlaylist({list: playlist, listType: "playlist", index: current.video.index ?? 0});
            }
            video.startWhenVisible();
          },
          onAutoplayBlocked() {
            if (embedded === video) showVideoStatus("Click Sync to room to play.");
          },
          onError(event) {
            if (embedded !== video) return;
            if (playlist) {
              window.clearTimeout(video.errorRecovery);
              video.errorRecovery = window.setTimeout(() => recoverPlaylist(event.data), 150);
            } else showYouTubeError(event.data);
          },
          onStateChange(event) {
            if (embedded !== video) return;
            if (playlist && event.data === 5 && event.target.getPlaylist()?.length) {
              video.playlistReady = true;
              video.startWhenVisible();
            }
            if (event.data === 1) {
              window.clearTimeout(video.errorRecovery);
              if (!video.visible || document.hidden) video.pauseForVisibility();
              else if (playlist) void reportPlaylistPosition(video);
            }
            if (event.data === 0 && (!playlist || event.target.getPlaylistIndex() + 1 >= event.target.getPlaylist()?.length)) {
              showVideoStatus(playlist ? "This room playlist has ended." : "This room video has ended.");
            }
          },
        },
      });
    }).catch(() => {
      if (embedded === video) {
        showVideoStatus("YouTube controls are unavailable. Use the controls in the player.");
      }
    });
  }

  watchVideo.addEventListener("click", () => {
    audioEnabled = true;
    videoHidden = false;
    const alreadyOpen = Boolean(embedded);
    unlockAudio();
    if (!alreadyOpen) return;
    if (embedded?.ready) playYouTube(embedded);
    else if (!embedded || track?.video?.provider === "bilibili") openVideo();
  });
  closeVideo.addEventListener("click", () => {
    videoHidden = true;
    removeVideo();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) embedded?.pauseForVisibility?.();
    else embedded?.startWhenVisible?.();
  });

  videoForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (!session || uploading || sharingVideo) return;
    const target = session;
    sharingVideo = true;
    refreshControls();
    videoRequestStatus.textContent = "Sharing video…";
    videoRequestStatus.hidden = false;
    try {
      const link = new URL(videoUrl.value.trim()).href;
      const response = await fetch(musicUrl() + "/video", {
        method: "POST",
        headers: {"X-Player-Id": target.playerId, "X-Video-Url": link},
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Couldn't share this video.");
      if (target !== session) return;
      videoUrl.value = "";
      videoRequestStatus.hidden = true;
      controls.open = false;
      if (track?.id === result.trackId) {
        audioEnabled = true;
        videoHidden = false;
        if (!embedded) openVideo();
        videoSection.scrollIntoView({block: "nearest"});
      } else {
        sharedVideoId = result.trackId;
      }
    } catch (error) {
      if (target === session) videoRequestStatus.textContent = error.message;
    } finally {
      if (target === session) {
        sharingVideo = false;
        refreshControls();
      }
    }
  });

  fileInput.addEventListener("change", () => {
    uploadStatus.hidden = true;
    refreshControls();
  });
  document.addEventListener("click", unlockAudio, {capture: true});
  play.addEventListener("click", async () => {
    const file = fileInput.files[0];
    if (!session || !file || uploading || sharingVideo) return;
    if (!file.name.toLowerCase().endsWith(".mp3") || file.size === 0 || file.size > 8 * 1024 * 1024) {
      uploadStatus.textContent = "Choose an MP3 up to 8 MB.";
      uploadStatus.hidden = false;
      return;
    }
    const target = session;
    uploading = true;
    refreshControls();
    uploadStatus.textContent = "Preparing music…";
    uploadStatus.hidden = false;
    try {
      const audio = getContext();
      const buffer = await audio.decodeAudioData(await file.arrayBuffer()).catch(() => {
        throw new Error("Choose a playable MP3.");
      });
      if (target !== session) return;
      if (buffer.duration > 15 * 60) throw new Error("Choose an MP3 up to 15 minutes long.");
      uploadStatus.textContent = "Uploading…";
      const response = await fetch(musicUrl(), {
        method: "POST",
        headers: {
          "Content-Type": "audio/mpeg",
          "X-Player-Id": session.playerId,
          "X-Track-Duration": String(buffer.duration),
          "X-Track-Title": encodeURIComponent(Array.from(file.name.slice(0, -4)).slice(0, 120).join("")),
        },
        body: file,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Couldn't play this track.");
      if (target !== session) return;
      fileInput.value = "";
      uploadStatus.hidden = true;
    } catch (error) {
      if (target === session) uploadStatus.textContent = error.message || "Couldn't play this track.";
    } finally {
      if (target === session) {
        uploading = false;
        refreshControls();
      }
    }
  });

  applyMusicVolume();
  applyEffectsVolume();
  return {update, reset, playEffect};
})();
