const { app, BrowserWindow, Menu, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

const SERVER_PORT = 8000;
const PROJECT_DIR = path.resolve(__dirname, '..');
let serverProcess = null;
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

function startServer() {
  const python = findPython();
  serverProcess = spawn(python, ['-m', 'uvicorn', 'tavern_rpg_engine:app', '--host', '127.0.0.1', '--port', String(SERVER_PORT)], {
    cwd: PROJECT_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  serverProcess.stdout.on('data', d => process.stdout.write(`[server] ${d}`));
  serverProcess.stderr.on('data', d => process.stderr.write(`[server] ${d}`));
  serverProcess.on('close', code => {
    console.log(`[server] exited with code ${code}`);
    serverProcess = null;
  });
}

function waitForServer(port, timeout = 30000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    function attempt() {
      if (Date.now() - start > timeout) return reject(new Error('Server start timeout'));
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

function createWindow() {
  const menu = Menu.buildFromTemplate([
    {
      label: '文件',
      submenu: [
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
    title: '酒馆 RPG — 多Agent推演系统',
    backgroundColor: '#1b1b1f',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${SERVER_PORT}`);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

app.whenReady().then(async () => {
  startServer();
  try {
    await waitForServer(SERVER_PORT);
    console.log('[OK] Server is ready');
  } catch (e) {
    console.error('[FAIL]', e.message);
  }
  createWindow();
});

app.on('window-all-closed', () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
  app.quit();
});

app.on('before-quit', () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
});
