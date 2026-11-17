import { DiscordSDK } from "/jsdelivr/npm/@discord/embedded-app-sdk@1.9.0/+esm";

const config = window.WATCH_PARTY_CONFIG || {};
const status = document.querySelector("#status");
const emptyState = document.querySelector("#empty-state");
const playerSection = document.querySelector("#player-section");
const title = document.querySelector("#video-title");
const toggle = document.querySelector("#toggle");
const back = document.querySelector("#back");
const forward = document.querySelector("#forward");

let channelId = null;
let socket = null;
let player = null;
let activeVideoId = null;
let latestState = null;
let applyingRemoteState = false;
let youtubeReady = null;

function setStatus(message) {
  status.textContent = message;
}

function videoIdFromUrl(value) {
  try {
    const url = new URL(value);
    if (url.hostname === "youtu.be") return url.pathname.slice(1);
    if (url.hostname.endsWith("youtube.com")) {
      return url.searchParams.get("v") || url.pathname.split("/").filter(Boolean).pop();
    }
  } catch (_) {
    return null;
  }
  return null;
}

function websocketUrl() {
  const url = new URL("/activity/ws", window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

function sendSync() {
  if (!player || !socket || socket.readyState !== WebSocket.OPEN) return;
  const state = player.getPlayerState();
  socket.send(JSON.stringify({
    type: "sync",
    position: player.getCurrentTime(),
    playing: state === window.YT.PlayerState.PLAYING,
  }));
}

function loadYoutubeApi() {
  if (youtubeReady) return youtubeReady;
  youtubeReady = new Promise((resolve) => {
    window.onYouTubeIframeAPIReady = resolve;
    const script = document.createElement("script");
    script.src = "/youtube/iframe_api";
    document.head.append(script);
  });
  return youtubeReady;
}

async function createPlayer(videoId) {
  await loadYoutubeApi();
  if (player) player.destroy();
  activeVideoId = videoId;
  player = new window.YT.Player("player", {
    videoId,
    host: `${window.location.origin}/youtube`,
    playerVars: {
      autoplay: 0,
      controls: 1,
      origin: config.publicUrl || window.location.origin,
      rel: 0,
    },
    events: {
      onReady: () => applyState(latestState),
      onStateChange: (event) => {
        if (applyingRemoteState) return;
        if ([window.YT.PlayerState.PLAYING, window.YT.PlayerState.PAUSED].includes(event.data)) {
          sendSync();
        }
      },
    },
  });
}

async function applyState(state) {
  latestState = state;
  if (!state?.video_url) {
    emptyState.hidden = false;
    playerSection.hidden = true;
    return;
  }

  const videoId = videoIdFromUrl(state.video_url);
  if (!videoId) {
    setStatus("지원하지 않는 YouTube URL이에요.");
    return;
  }
  emptyState.hidden = true;
  playerSection.hidden = false;
  title.textContent = state.title || "YouTube 같이 보기";

  if (!player || activeVideoId !== videoId) {
    await createPlayer(videoId);
    return;
  }

  applyingRemoteState = true;
  const current = player.getCurrentTime();
  if (Math.abs(current - state.position) > 1.5) player.seekTo(state.position, true);
  if (state.playing) {
    player.playVideo();
    toggle.textContent = "일시정지";
  } else {
    player.pauseVideo();
    toggle.textContent = "재생";
  }
  window.setTimeout(() => { applyingRemoteState = false; }, 250);
}

function connectSocket() {
  socket = new WebSocket(websocketUrl());
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify({ type: "join", channel_id: channelId }));
    setStatus("같이 보기와 동기화됨");
  });
  socket.addEventListener("message", async (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "state") await applyState(payload);
    if (payload.type === "error") setStatus(payload.message);
  });
  socket.addEventListener("close", () => {
    setStatus("연결이 끊겼어요. 다시 연결하는 중…");
    window.setTimeout(connectSocket, 1500);
  });
}

async function start() {
  if (!config.clientId) throw new Error("Discord application ID를 읽지 못했어요.");
  const discord = new DiscordSDK(config.clientId);
  await discord.ready();
  channelId = discord.channelId;
  if (!channelId) throw new Error("음성 채널 안에서 Activity를 실행해 주세요.");
  connectSocket();

  toggle.addEventListener("click", () => {
    if (!player) return;
    if (player.getPlayerState() === window.YT.PlayerState.PLAYING) player.pauseVideo();
    else player.playVideo();
    sendSync();
  });
  back.addEventListener("click", () => {
    if (!player) return;
    player.seekTo(Math.max(0, player.getCurrentTime() - 10), true);
    sendSync();
  });
  forward.addEventListener("click", () => {
    if (!player) return;
    player.seekTo(player.getCurrentTime() + 10, true);
    sendSync();
  });
  window.setInterval(() => {
    if (player?.getPlayerState() === window.YT.PlayerState.PLAYING) sendSync();
  }, 3000);
}

start().catch((error) => setStatus(error.message || "Activity를 시작하지 못했어요."));
