// Podroom desktop: starts the bundled Python server on this computer and shows the studio in its own window.
// Nothing leaves the machine except the AI calls you configure; the server only listens on 127.0.0.1.
const { app, BrowserWindow, Menu, dialog, session, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

// A fixed port when it is free, so the page's own preferences (kept per address) survive restarts.
const PREFERRED_PORT = 17860;
const HOMEPAGE = "https://github.com/AronMgH/podroom";

let server = null, win = null, origin = "", quitting = false;
const logLines = [];

const firstInstance = app.requestSingleInstanceLock();
if (!firstInstance) app.quit(); // a second launch only brings the open window forward
app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });

// Your show, episodes and downloaded models. Kept apart from Electron's own files in userData.
const dataDir = () => (app.isPackaged ? path.join(app.getPath("userData"), "data") : process.env.PODROOM_DATA || "");

function freePort(port) {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.once("error", () => resolve(0));
    s.listen(port, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

function serverCommand() {
  if (app.isPackaged) {
    const exe = process.platform === "win32" ? "podroom-server.exe" : "podroom-server";
    return [path.join(process.resourcesPath, "server", exe), []];
  }
  // Development (npm start): the checkout's own Python and server.py.
  const root = path.join(__dirname, "..");
  const py = process.platform === "win32" ? path.join(root, ".venv", "Scripts", "python.exe") : path.join(root, ".venv", "bin", "python");
  return [py, [path.join(root, "server.py")]];
}

function startServer(port) {
  const [cmd, args] = serverCommand();
  const logFile = fs.createWriteStream(path.join(app.getPath("userData"), "server.log"));
  server = spawn(cmd, args, {
    env: { ...process.env, PODROOM_PORT: String(port), PODROOM_DATA: dataDir(), PODROOM_EXIT_WITH_STDIN: "1", PYTHONUNBUFFERED: "1" },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  const keep = (chunk) => {
    logFile.write(chunk);
    logLines.push(...chunk.toString().split("\n").filter(Boolean));
    logLines.splice(0, Math.max(0, logLines.length - 40));
  };
  server.stdout.on("data", keep);
  server.stderr.on("data", keep);
  server.on("exit", (code) => {
    server = null;
    if (quitting) return;
    dialog.showErrorBox("Podroom stopped", `The Podroom server stopped (exit code ${code}).\n\n${logLines.slice(-12).join("\n")}`);
    app.quit();
  });
}

function ready(url, timeoutMs) {
  const until = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      http.get(url, (res) => { res.resume(); resolve(); }).on("error", () => {
        if (!server) return reject(new Error("the server exited while starting"));
        if (Date.now() > until) return reject(new Error("the server did not start in time"));
        setTimeout(tryOnce, 200);
      });
    };
    tryOnce();
  });
}

const isOurs = (url) => !!origin && (url === origin || url.startsWith(origin + "/"));

function createWindow() {
  win = new BrowserWindow({
    width: 1360, height: 900, minWidth: 380, minHeight: 500,
    title: "Podroom", backgroundColor: "#0b0b0d", autoHideMenuBar: true, show: false,
    icon: path.join(__dirname, "build", "icon.png"),
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
  });
  win.once("ready-to-show", () => win.show());
  // Links to anywhere else (OpenRouter, Hugging Face, the README) open in your browser, not in the app.
  win.webContents.setWindowOpenHandler(({ url }) => { if (!isOurs(url)) shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => {
    if (!isOurs(url) && !url.startsWith("file:")) { e.preventDefault(); shell.openExternal(url); }
  });
  win.on("closed", () => { win = null; });
  return win.loadFile(path.join(__dirname, "splash.html"));
}

function buildMenu() {
  const help = {
    label: "Help",
    submenu: [
      { label: "Open data folder", click: () => shell.openPath(dataDir() || path.join(__dirname, "..")) },
      { label: "Open server log", click: () => shell.openPath(path.join(app.getPath("userData"), "server.log")) },
      { type: "separator" },
      { label: "Podroom on GitHub", click: () => shell.openExternal(HOMEPAGE) },
    ],
  };
  const template = [
    ...(process.platform === "darwin" ? [{ role: "appMenu" }] : [{ role: "fileMenu" }]),
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { label: "Studio", accelerator: "CmdOrCtrl+1", click: () => origin && win?.loadURL(origin + "/") },
        { label: "Casts", accelerator: "CmdOrCtrl+2", click: () => origin && win?.loadURL(origin + "/casts") },
        { label: "Settings", accelerator: "CmdOrCtrl+,", click: () => origin && win?.loadURL(origin + "/settings") },
        { type: "separator" },
        { role: "reload" }, { role: "toggleDevTools" }, { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { type: "separator" }, { role: "togglefullscreen" },
      ],
    },
    { role: "windowMenu" },
    help,
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  if (!firstInstance) return;
  fs.mkdirSync(app.getPath("userData"), { recursive: true });
  // The mic is the only permission the studio asks for, and only its own pages may have it.
  const allow = (permission, url) => permission === "media" && isOurs(url || "");
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb, details) => cb(allow(permission, details.requestingUrl || wc.getURL())));
  session.defaultSession.setPermissionCheckHandler((wc, permission, requestingOrigin) => allow(permission, requestingOrigin));

  buildMenu();
  await createWindow();
  const port = (await freePort(PREFERRED_PORT)) || (await freePort(0));
  origin = `http://127.0.0.1:${port}`;
  startServer(port);
  try {
    await ready(origin + "/api/stt", 120000);
    if (win) await win.loadURL(origin + "/");
  } catch (e) {
    if (quitting) return;
    dialog.showErrorBox("Podroom could not start", `${e.message}.\n\n${logLines.slice(-12).join("\n")}`);
    app.quit();
  }
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  quitting = true;
  if (server) { server.stdin.end(); server.kill(); }
});
