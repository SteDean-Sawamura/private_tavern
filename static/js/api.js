/* api.js -- thin wrappers around the RPG map UI backend endpoints */

const BASE = "";

async function _json(res) {
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "HTTP " + res.status);
  }
  return res.json();
}

function _post(url, body) {
  return fetch(BASE + url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(_json);
}

function _get(url) {
  return fetch(BASE + url).then(_json);
}

// ----- state -----
export function getState() { return _get("/api/state"); }

// ----- scripts -----
export function getScripts() { return _get("/api/scripts"); }
export function getScript(id) { return _get("/api/scripts/" + id); }

// ----- AI profiles -----
export function getAIProfiles() { return _get("/api/ai-profiles"); }

// ----- saves -----
export function getSaves() { return _get("/api/saves"); }
export function loadSave(id) { return _post("/api/load", { id: id }); }
export function deleteSave(id) { return _post("/api/delete-save", { id: id }); }
export function saveGame() { return _post("/api/save", {}); }

// ----- game lifecycle -----
export function startGame(payload) { return _post("/api/start", payload); }
export function resetGame() { return _post("/api/reset", {}); }

// ----- turn / confirm -----
export function submitTurn(action) { return _post("/api/turn", { action: action }); }
export function confirm(body) { return _post("/api/confirm", body || {}); }

// ----- NPC talk -----
export function talkToNpc(npcId, message) { return _post("/api/talk", { npc_id: npcId, message: message }); }

// ----- retry NPC -----
export function retryNpc(npcId, hint) { return _post("/api/retry-npc/" + npcId, { hint: hint }); }

// ----- scene regenerate / swipe -----
export function regenerate() { return _post("/api/regenerate", {}); }
export function swipe(direction) { return _post("/api/swipe", { direction: direction }); }

// ----- create mode -----
export function createStart(theme) { return _post("/api/create/start", { theme: theme }); }
export function createNext(input) { return _post("/api/create/next", { input: input }); }
