const { app, BrowserWindow, Menu, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

const TAVERN_PORT = 8000;
const RPG_PORT = 8080;
const PROJECT_DIR = path.resolve(__dirname, '..');

let tavernProcess = null;
let rpgProcess = null;
let mainWindow = null;

function findPython() {
  const venvPython = path.join(PROJECT_DIR, 'bar', 'Scripts', 'python.exe');
  try {
    require('fs').accessSync(venvPython);
    return venvPython;
  } catch {
    return 'python';
  }
}

function startProcess(script, label) {
  const python = findPython();
  const proc = spawn(python, [script], {
    cwd: PROJECT_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  proc.stdout.on('data', d => process.stdout.write(`[${label}] ${d}`));
  proc.stderr.on('data', d => process.stderr.write(`[${label}] ${d}`));
  proc.on('close', code => console.log(`[${label}] exited (${code})`));
  return proc;
}

function waitForServer(port, timeout = 30000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    function attempt() {
      if (Date.now() - start > timeout) return reject(new Error(`Port ${port} timeout`));
      const sock = new net.Socket();
      sock.setTimeout(500);
      sock.once('connect', () => { sock.destroy(); resolve(); });
      sock.once('error', () => { sock.destroy(); setTimeout(attempt, 300); });
      sock.once('timeout', () => { sock.destroy(); setTimeout(attempt, 300); });
      sock.connect(port, '127.0.0.1');
    }
    attempt();
  });
}

function navigateTo(port) {
  if (mainWindow) mainWindow.loadURL(`http://127.0.0.1:${port}`);
}

function createWindow() {
  const menu = Menu.buildFromTemplate([
    {
      label: '应用',
      submenu: [
        { label: '酒馆（对话模式）', accelerator: 'CmdOrCtrl+1', click: () => navigateTo(TAVERN_PORT) },
        { label: 'RPG 推演系统', accelerator: 'CmdOrCtrl+2', click: () => navigateTo(RPG_PORT) },
        { type: 'separator' },
        { label: '重新加载', accelerator: 'CmdOrCtrl+R', click: () => mainWindow?.reload() },
        { label: '开发者工具', accelerator: 'F12', click: () => mainWindow?.webContents.toggleDevTools() },
        { type: 'separator' },
        { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '视图',
      submenu: [
        { label: '放大', accelerator: 'CmdOrCtrl+=', click: () => mainWindow?.webContents.setZoomLevel(mainWindow.webContents.getZoomLevel() + 0.5) },
        { label: '缩小', accelerator: 'CmdOrCtrl+-', click: () => mainWindow?.webContents.setZoomLevel(mainWindow.webContents.getZoomLevel() - 0.5) },
        { label: '重置缩放', accelerator: 'CmdOrCtrl+0', click: () => mainWindow?.webContents.setZoomLevel(0) },
        { type: 'separator' },
        { label: '全屏', accelerator: 'F11', click: () => mainWindow?.setFullScreen(!mainWindow?.isFullScreen()) },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: '酒馆',
    backgroundColor: '#1b1b1f',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${TAVERN_PORT}`);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

function killAll() {
  if (tavernProcess) { tavernProcess.kill(); tavernProcess = null; }
  if (rpgProcess) { rpgProcess.kill(); rpgProcess = null; }
}

app.whenReady().then(async () => {
  tavernProcess = startProcess('app.py', 'tavern');
  rpgProcess = startProcess('tavern_rpg_engine.py', 'rpg');

  try {
    await Promise.all([
      waitForServer(TAVERN_PORT),
      waitForServer(RPG_PORT),
    ]);
    console.log('[OK] Both servers ready');
  } catch (e) {
    console.error('[WARN]', e.message, '- opening anyway');
  }

  createWindow();
});

app.on('window-all-closed', () => { killAll(); app.quit(); });
app.on('before-quit', killAll);
