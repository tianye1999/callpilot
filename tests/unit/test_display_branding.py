"""Display branding must not rename persistent or executable identities."""

import ast
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_display_branding_preserves_internal_identity(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    import tray_app
    from agentcall import cloud_credentials, config, macos_launchd, macos_service_management

    def read(path):
        return (ROOT / path).read_text(encoding="utf-8")

    for path in (
        "src/agentcall/web/static/index.html",
        "src/agentcall/web/static/manifest.webmanifest",
        "src/agentcall/web/static/remote_dialer.html",
        "src/agentcall/web/static/remote_dialer.js",
    ):
        assert "CallPilot" not in read(path), path

    for path in (
        "tray_app.py", "desktop_app.py", "src/agentcall/macos_shell.py",
        "src/agentcall/macos_window.py", "src/agentcall/notification_events.py",
        "src/agentcall/macos_notifications.py", "src/agentcall/macos_notification_sender.py",
        "src/agentcall/app_updates.py",
    ):
        tree = ast.parse(read(path))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        # rumps uses its name as the data directory; log text is not UI copy.
        internal = {"CallPilot", "已有 CallPilot 菜单栏实例在运行，本实例让位退出"} if path == "tray_app.py" else set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert "CallPilot" not in node.value or node.value in internal, (path, node.lineno, node.value)

    for lang, name in (("zh", "KinyAI-SIM助手"), ("en", "KinyAI SIM Assistant")):
        assert name in tray_app.menu_label("open_app", lang)
        assert name in tray_app.menu_label("quit", lang)
    manifest = json.loads(read("src/agentcall/web/static/manifest.webmanifest"))
    assert manifest["name"] == "KinyAI-SIM助手"
    assert manifest["short_name"] == "KinyAI SIM"
    assert [icon["src"] for icon in manifest["icons"]] == ["/callpilot-192.png", "/callpilot-512.png"]

    spec = ast.parse(read("packaging/agentcall.spec"))
    calls = {node.func.id: node for node in ast.walk(spec)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {"EXE", "COLLECT", "BUNDLE"}}
    keywords = {name: {kw.arg: kw.value for kw in call.keywords} for name, call in calls.items()}
    assert ast.literal_eval(keywords["EXE"]["name"]) == "CallPilot"
    assert ast.literal_eval(keywords["COLLECT"]["name"]) == "CallPilot"
    assert ast.literal_eval(keywords["BUNDLE"]["name"]) == "CallPilot.app"
    assert ast.literal_eval(keywords["BUNDLE"]["bundle_identifier"]) == "ai.bondings.callpilot"
    info = keywords["BUNDLE"]["info_plist"]
    values = {ast.literal_eval(key): value for key, value in zip(info.keys, info.values)}
    assert ast.literal_eval(values["CFBundleDisplayName"]) == "KinyAI-SIM助手"
    assert ast.literal_eval(values["CFBundleName"]) == "KinyAI SIM"
    # PyInstaller derives CFBundleExecutable from EXE.name, not CFBundleName.
    assert "CFBundleExecutable" not in values or ast.literal_eval(values["CFBundleExecutable"]) == "CallPilot"

    assert config.APP_NAME == "CallPilot"
    assert cloud_credentials._SERVICE == "ai.bondings.callpilot.cloud"
    assert (cloud_credentials._ACCOUNT, cloud_credentials._KEY_ACCOUNT) == ("edge-credential", "edge-device-key")
    monkeypatch.delenv("AGENTCALL_APP_SUPPORT_DIR", raising=False)
    assert macos_launchd.app_support_dir() == Path.home() / "Library/Application Support/CallPilot"
    assert macos_launchd.LAUNCH_LABELS == ("com.agentcall.bridge", "com.agentcall.app", "com.agentcall.tray")
    for plist in macos_service_management.bundled_plists().values():
        assert plist["BundleProgram"] == "Contents/MacOS/CallPilot"
        assert plist["ProgramArguments"][0] == "CallPilot"
        assert plist["AssociatedBundleIdentifiers"] == ["ai.bondings.callpilot"]
    layout = macos_launchd.make_layout("/Applications/CallPilot.app/Contents/MacOS/CallPilot", uid=123)
    for label, plist in macos_launchd.build_plists(layout).items():
        suffix = label.rsplit(".", 1)[1]
        assert str(layout.executable) in plist["ProgramArguments"]
        assert plist["StandardOutPath"] == str(layout.log_dir / f"launchd-{suffix}.out.log")
        assert plist["StandardErrorPath"] == str(layout.log_dir / f"launchd-{suffix}.err.log")
    tray = ast.parse(read("tray_app.py"))
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "__init__" and node.args
               and isinstance(node.args[0], ast.Constant) and node.args[0].value == "CallPilot"
               for node in ast.walk(tray))
    for setting in ("REMOTE_CLOUD_ENABLED", "REMOTE_CLOUD_URL", "REMOTE_CLOUD_DIALER_URL"):
        assert "CallPilot" not in config.get_spec(setting).label
    assert tray_app.DEFAULT_WEB_URL == "http://127.0.0.1:47100"
    assert '"WEB_PORT", "Web 监听端口", "int", "47100"' in read("src/agentcall/config.py")
    assert 'DMG_PATH="$DIST_DIR/CallPilot-$VERSION.dmg"' in read("packaging/build_installer.sh")
    assert 'APP_PATH="$DIST_DIR/CallPilot.app"' in read("packaging/build_installer.sh")
    assert "/Applications/CallPilot.app/Contents/MacOS/CallPilot" in read("scripts/launchd/com.agentcall.app.plist")
    assert '"Library/Application Support/CallPilot"' in read("src/agentcall/macos_uninstall.py")
    assert tomllib.loads(read("pyproject.toml"))["project"]["name"] == "callpilot"
