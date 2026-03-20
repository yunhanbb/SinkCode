import json
import os
import queue
import re
import shlex
import signal
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

import lark_oapi as lark
import pexpect
from dotenv import load_dotenv
from lark_oapi.api.drive.v1 import (
    GetPermissionPublicRequest,
    PatchPermissionPublicRequest,
    PermissionPublicRequest,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetImageRequest,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.api.docx.v1 import (
    BatchDeleteDocumentBlockChildrenRequest,
    BatchDeleteDocumentBlockChildrenRequestBody,
    ConvertDocumentRequest,
    ConvertDocumentRequestBody,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    GetDocumentRequest,
    GetDocumentBlockChildrenRequest,
)
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1


ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MENTION_TOKEN_RE = re.compile(r"@_[^\s]+")
DOCX_CREATE_CHILDREN_BATCH_SIZE = 50
DOCX_WRITE_INTERVAL_SECONDS = 0.4
CARD_MARKDOWN_CHUNK_CHARS = 1800
CARD_MAX_MARKDOWN_CHUNKS = 18
CARD_MAX_COMMAND_BLOCKS = 6
CARD_MAX_BODY_ELEMENTS = 120
CARD_HEADER_TEXT_LIMIT = 80
CARD_SUMMARY_TEXT_LIMIT = 60


@dataclass
class Config:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    shell_path: str
    state_file: Path
    log_file: Path
    history_dir: Path
    flush_interval_seconds: float
    codex_flush_interval_seconds: float
    card_update_interval_seconds: float
    card_status_max_lines: int
    card_answer_max_chars: int
    typewriter_status_chars_per_tick: int
    typewriter_answer_chars_per_tick: int
    max_message_chars: int
    allowed_prefixes: tuple[str, ...]
    ca_cert_path: Optional[Path]
    insecure_skip_verify: bool
    card_template_id: str
    general_card_template_id: str
    codex_card_template_id: str
    math_card_template_id: str
    card_template_var_name: str
    card_status_var_name: str
    card_answer_var_name: str
    client_name: str
    math_tutor_system_prompt: str
    math_tutor_doc_folder_token: str
    math_tutor_doc_title: str

    @staticmethod
    def load() -> "Config":
        load_dotenv()
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip()
        if not app_id or not app_secret or not verification_token:
            raise ValueError("Missing FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN")

        prefixes = tuple(
            p.strip()
            for p in os.getenv("ALLOWED_COMMAND_PREFIXES", "").split(",")
            if p.strip()
        )
        ca_cert_raw = os.getenv("FEISHU_CA_CERT_PATH", "").strip()
        ca_cert_path = Path(ca_cert_raw).expanduser() if ca_cert_raw else None
        insecure_skip_verify = os.getenv("FEISHU_INSECURE_SKIP_VERIFY", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        return Config(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", "").strip(),
            shell_path=os.getenv("SHELL_PATH", "/bin/zsh").strip(),
            state_file=Path(os.getenv("STATE_FILE", ".bridge_state.json")).expanduser(),
            log_file=Path(os.getenv("LOG_FILE", "bridge.log")).expanduser(),
            history_dir=Path(os.getenv("HISTORY_DIR", "history")).expanduser(),
            flush_interval_seconds=float(os.getenv("FLUSH_INTERVAL_SECONDS", "1.2")),
            codex_flush_interval_seconds=float(os.getenv("CODEX_FLUSH_INTERVAL_SECONDS", "0.35")),
            card_update_interval_seconds=float(os.getenv("CARD_UPDATE_INTERVAL_SECONDS", "0.8")),
            card_status_max_lines=int(os.getenv("CARD_STATUS_MAX_LINES", "18")),
            card_answer_max_chars=int(os.getenv("CARD_ANSWER_MAX_CHARS", "2600")),
            typewriter_status_chars_per_tick=int(os.getenv("TYPEWRITER_STATUS_CHARS_PER_TICK", "8")),
            typewriter_answer_chars_per_tick=int(os.getenv("TYPEWRITER_ANSWER_CHARS_PER_TICK", "24")),
            max_message_chars=int(os.getenv("MAX_MESSAGE_CHARS", "1200")),
            allowed_prefixes=prefixes,
            ca_cert_path=ca_cert_path,
            insecure_skip_verify=insecure_skip_verify,
            card_template_id=os.getenv("CARD_TEMPLATE_ID", "").strip(),
            general_card_template_id=os.getenv("GENERAL_CARD_TEMPLATE_ID", "").strip(),
            codex_card_template_id=os.getenv("CODEX_CARD_TEMPLATE_ID", "").strip(),
            math_card_template_id=os.getenv("MATH_CARD_TEMPLATE_ID", "").strip(),
            card_template_var_name=os.getenv("CARD_TEMPLATE_VAR_NAME", "content").strip() or "content",
            card_status_var_name=os.getenv("CARD_STATUS_VAR_NAME", "status_content").strip(),
            card_answer_var_name=os.getenv("CARD_ANSWER_VAR_NAME", "answer_content").strip(),
            client_name=os.getenv("OPENCODEX_CLIENT_NAME", "").strip() or socket.gethostname().strip() or "OpenCodex",
            math_tutor_system_prompt=os.getenv("MATH_TUTOR_SYSTEM_PROMPT", "").strip(),
            math_tutor_doc_folder_token=os.getenv("MATH_TUTOR_DOC_FOLDER_TOKEN", "").strip(),
            math_tutor_doc_title=os.getenv("MATH_TUTOR_DOC_TITLE", "OpenCodex 数学辅导总结").strip() or "OpenCodex 数学辅导总结",
        )


class BridgeState:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.authorized_open_id: Optional[str] = None
        self.bound_chat_id: Optional[str] = None
        self.client_id: str = ""
        self.selected_chat_id: str = ""
        self.math_tutor_system_prompt: str = ""
        self.math_summary_doc_id: str = ""
        self.math_summary_doc_title: str = ""
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.authorized_open_id = raw.get("authorized_open_id")
            self.bound_chat_id = raw.get("bound_chat_id")
            self.client_id = str(raw.get("client_id") or "")
            self.selected_chat_id = str(raw.get("selected_chat_id") or "")
            self.math_tutor_system_prompt = str(raw.get("math_tutor_system_prompt") or "")
            self.math_summary_doc_id = str(raw.get("math_summary_doc_id") or "")
            self.math_summary_doc_title = str(raw.get("math_summary_doc_title") or "")
        except Exception:
            pass

    def ensure_client_identity(self) -> str:
        with self._lock:
            if not self.client_id:
                self.client_id = uuid4().hex[:8]
                self._save_locked()
            return self.client_id

    def bind(self, open_id: str, chat_id: str) -> None:
        with self._lock:
            self.authorized_open_id = open_id
            self.bound_chat_id = chat_id
            self.selected_chat_id = ""
            self._save_locked()

    def select_chat(self, chat_id: str) -> None:
        with self._lock:
            self.selected_chat_id = chat_id.strip()
            self._save_locked()

    def clear_selected_chat(self, chat_id: str = "") -> bool:
        with self._lock:
            if chat_id and self.selected_chat_id != chat_id:
                return False
            if not self.selected_chat_id:
                return False
            self.selected_chat_id = ""
            self._save_locked()
            return True

    def is_selected_chat(self, chat_id: Optional[str]) -> bool:
        return bool(chat_id) and chat_id == self.selected_chat_id

    def set_math_tutor_system_prompt(self, prompt: str) -> None:
        with self._lock:
            self.math_tutor_system_prompt = prompt.strip()
            self._save_locked()

    def set_math_summary_doc(self, document_id: str, title: str = "") -> None:
        with self._lock:
            self.math_summary_doc_id = document_id.strip()
            self.math_summary_doc_title = title.strip()
            self._save_locked()

    def clear_math_summary_doc(self) -> None:
        with self._lock:
            self.math_summary_doc_id = ""
            self.math_summary_doc_title = ""
            self._save_locked()

    def is_authorized(self, open_id: Optional[str]) -> bool:
        if not self.authorized_open_id:
            return True
        return bool(open_id) and open_id == self.authorized_open_id

    def _save_locked(self) -> None:
        payload = {
            "authorized_open_id": self.authorized_open_id,
            "bound_chat_id": self.bound_chat_id,
            "client_id": self.client_id,
            "selected_chat_id": self.selected_chat_id,
            "math_tutor_system_prompt": self.math_tutor_system_prompt,
            "math_summary_doc_id": self.math_summary_doc_id,
            "math_summary_doc_title": self.math_summary_doc_title,
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class CodexTask:
    task_id: str
    chat_id: str
    sender_open_id: str
    prompt: str
    status: str
    started_at: str
    updated_at: str
    finished_at: str
    commands: list[str]
    answer_parts: list[str]
    events: list[str]
    card_message_id: str
    last_card_push_ts: float


@dataclass
class MathTutorProblem:
    problem_id: str
    problem_index: int
    chat_id: str
    sender_open_id: str
    status: str
    started_at: str
    updated_at: str
    finished_at: str
    thread_id: str
    card_message_id: str
    last_card_push_ts: float
    user_inputs: list[str]
    answer_parts: list[str]
    image_paths: list[str]
    doc_synced: bool
    doc_document_id: str
    doc_start_index: int
    doc_block_count: int


class HistoryStore:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.tasks_dir = base_dir / "tasks"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create_task(self, chat_id: str, sender_open_id: str, prompt: str) -> str:
        task_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        now = self._now()
        payload = {
            "task_id": task_id,
            "chat_id": chat_id,
            "sender_open_id": sender_open_id,
            "prompt": prompt,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "finished_at": "",
            "commands": [],
            "answer_parts": [],
            "events": [],
            "card_message_id": "",
        }
        self._save(task_id, payload)
        return task_id

    def update_task(self, task_id: str, mutator) -> None:
        with self._lock:
            data = self._load(task_id)
            if not data:
                return
            mutator(data)
            data["updated_at"] = self._now()
            self._save(task_id, data, locked=True)

    def list_recent(self, limit: int = 10) -> list[dict]:
        files = sorted(self.tasks_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for fp in files[:max(1, limit)]:
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                out.append(
                    {
                        "task_id": data.get("task_id", ""),
                        "status": data.get("status", ""),
                        "started_at": data.get("started_at", ""),
                        "prompt": str(data.get("prompt", "")),
                    }
                )
            except Exception:
                continue
        return out

    def get_task(self, task_id: str) -> Optional[dict]:
        return self._load(task_id)

    def clear(self) -> int:
        count = 0
        for fp in self.tasks_dir.glob("*.json"):
            try:
                fp.unlink()
                count += 1
            except Exception:
                pass
        return count

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _load(self, task_id: str) -> Optional[dict]:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save(self, task_id: str, data: dict, locked: bool = False) -> None:
        def _write():
            self._task_path(task_id).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        if locked:
            _write()
        else:
            with self._lock:
                _write()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


class ShellSession:
    def __init__(self, shell_path: str):
        self.shell_path = shell_path
        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self.proc: Optional[pexpect.spawn] = None
        self._stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()

    def start(self) -> None:
        self.proc = pexpect.spawn(self.shell_path, ["-li"], encoding="utf-8", echo=False, codec_errors="ignore")
        self.proc.delaybeforesend = 0.02
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        assert self.proc is not None
        while not self._stop.is_set():
            try:
                chunk = self.proc.read_nonblocking(size=4096, timeout=0.2)
                if chunk:
                    self.output_queue.put(chunk)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                self.output_queue.put("\n[bridge] shell session ended.\n")
                break
            except Exception as exc:
                self.output_queue.put(f"\n[bridge] shell read error: {exc}\n")
                break

    def send_command(self, command: str) -> None:
        if not self.proc:
            raise RuntimeError("shell not started")
        with self._write_lock:
            self.proc.sendline(command)

    def send_ctrl_c(self) -> None:
        if not self.proc:
            return
        with self._write_lock:
            self.proc.sendintr()

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.isalive():
            try:
                self.proc.sendline("exit")
            except Exception:
                pass


class FeishuCodexBridge:
    def __init__(self, config: Config):
        self.config = config
        self._log_lock = threading.Lock()
        self._log_file = config.log_file
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log(f"[bridge] logfile: {self._log_file}")
        self.state = BridgeState(config.state_file)
        self.client_id = self.state.ensure_client_identity()
        self.client_name = config.client_name
        self.history = HistoryStore(config.history_dir)
        self.shell = ShellSession(config.shell_path)
        self.shutdown = threading.Event()
        self._apply_tls_config()

        self.api_client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        self.event_handler = (
            lark.EventDispatcherHandler.builder(config.encrypt_key, config.verification_token)
            .register_p2_im_message_receive_v1(self._on_receive_message)
            .build()
        )

        self.ws_client = lark.ws.Client(
            config.app_id,
            config.app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=self.event_handler,
        )

        self.forwarder_thread = threading.Thread(target=self._forward_terminal_output, daemon=True)
        self.codex_mode = False
        self._codex_setup_stage = ""
        self._pending_codex_model = ""
        self._pending_codex_model_display = ""
        self._codex_model = ""
        self._codex_model_display = "default"
        self._codex_permission = ""
        self.math_tutor_mode = False
        self._mobile_line_carry = ""
        self._active_task: Optional[CodexTask] = None
        self._math_problem: Optional[MathTutorProblem] = None
        self._math_problem_counter = 0
        self._math_runner_thread: Optional[threading.Thread] = None
        self._math_process: Optional[object] = None
        self._math_lock = threading.Lock()
        self._math_asset_dir = self.config.history_dir / "math_assets"
        self._math_asset_dir.mkdir(parents=True, exist_ok=True)
        self._general_card_message_id = ""
        self._general_feed_parts: list[str] = []
        self._session_card_message_id = ""
        self._session_status_parts: list[str] = []
        self._session_answer_parts: list[str] = []
        self._status_pending: list[str] = []
        self._answer_pending: list[str] = []
        self._status_typing_current = ""
        self._answer_typing_current = ""
        self._status_typing_index = 0
        self._answer_typing_index = 0
        self._math_status_parts: list[str] = []
        self._math_answer_parts: list[str] = []
        self._math_status_pending: list[str] = []
        self._math_answer_pending: list[str] = []
        self._math_status_typing_current = ""
        self._math_answer_typing_current = ""
        self._math_status_typing_index = 0
        self._math_answer_typing_index = 0
        self._last_math_doc_error = ""
        self._log(f"[bridge] client ready: {self._client_label()}")

    def _apply_tls_config(self) -> None:
        ssl_context: Optional[ssl.SSLContext] = None

        if self.config.ca_cert_path:
            if not self.config.ca_cert_path.exists():
                raise ValueError(f"FEISHU_CA_CERT_PATH does not exist: {self.config.ca_cert_path}")
            ca_file = str(self.config.ca_cert_path)
            os.environ["SSL_CERT_FILE"] = ca_file
            os.environ["REQUESTS_CA_BUNDLE"] = ca_file
            os.environ["CURL_CA_BUNDLE"] = ca_file
            ssl_context = ssl.create_default_context(cafile=ca_file)
            self._log(f"[bridge] TLS CA loaded: {ca_file}")

        if self.config.insecure_skip_verify:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            self._patch_requests_insecure()
            self._log("[bridge] WARNING: TLS verification disabled (FEISHU_INSECURE_SKIP_VERIFY=true)")

        if ssl_context is not None:
            self._patch_lark_ws_ssl(ssl_context)

    @staticmethod
    def _patch_lark_ws_ssl(ssl_context: ssl.SSLContext) -> None:
        import lark_oapi.ws.client as ws_client

        original_connect = ws_client.websockets.connect
        if getattr(original_connect, "_bridge_tls_patched", False):
            return

        def wrapped_connect(uri, *args, **kwargs):
            kwargs.setdefault("ssl", ssl_context)
            return original_connect(uri, *args, **kwargs)

        wrapped_connect._bridge_tls_patched = True
        ws_client.websockets.connect = wrapped_connect

    @staticmethod
    def _patch_requests_insecure() -> None:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        original_request = requests.sessions.Session.request
        if getattr(original_request, "_bridge_tls_patched", False):
            return

        def wrapped_request(session, method, url, **kwargs):
            kwargs.setdefault("verify", False)
            return original_request(session, method, url, **kwargs)

        wrapped_request._bridge_tls_patched = True
        requests.sessions.Session.request = wrapped_request

    def start(self) -> None:
        self.shell.start()
        self.forwarder_thread.start()
        self._send_bound_chat("[bridge] 已启动。发送 /help 查看命令。")
        self.ws_client.start()

    def stop(self) -> None:
        self.shutdown.set()
        self._interrupt_math_tutor()
        self.shell.stop()

    def _client_label(self) -> str:
        return f"{self.client_name} ({self.client_id})"

    def _is_selected_chat(self, chat_id: str) -> bool:
        return self.state.is_selected_chat(chat_id)

    def _selected_required(self, chat_id: str, normalized_text: str, message_type: str) -> bool:
        if self._is_selected_chat(chat_id):
            return True
        self._log(
            "[bridge] ignored unselected message: "
            f"chat_id={chat_id} client={self._client_label()} type={message_type} text={normalized_text!r}"
        )
        return False

    def _current_mode_label(self) -> str:
        if self._codex_setup_stage:
            return "codex-setup"
        if self.math_tutor_mode:
            return "math"
        if self.codex_mode:
            return "codex"
        return "shell"

    @staticmethod
    def _normalize_codex_model(raw: str) -> tuple[str, str]:
        value = raw.strip()
        if not value:
            return "", ""
        if value.lower() in {"default", "默认", "local-default"}:
            return "", "default"
        return value, value

    @staticmethod
    def _normalize_codex_permission(raw: str) -> str:
        value = raw.strip().lower()
        aliases = {
            "read-only": "read-only",
            "readonly": "read-only",
            "read_only": "read-only",
            "workspace-write": "workspace-write",
            "workspace_write": "workspace-write",
            "workspace": "workspace-write",
            "danger-full-access": "danger-full-access",
            "danger_full_access": "danger-full-access",
            "danger": "danger-full-access",
            "full-access": "danger-full-access",
        }
        return aliases.get(value, "")

    @staticmethod
    def _codex_permission_prompt() -> str:
        return "请输入 permission：`read-only` / `workspace-write` / `danger-full-access`"

    def _reset_codex_setup_state(self) -> None:
        self._codex_setup_stage = ""
        self._pending_codex_model = ""
        self._pending_codex_model_display = ""

    def _reset_codex_session_options(self) -> None:
        self._codex_model = ""
        self._codex_model_display = "default"
        self._codex_permission = ""

    def _begin_codex_setup(self) -> None:
        self.codex_mode = False
        self._reset_codex_card_state()
        self._reset_codex_session_options()
        self._reset_codex_setup_state()
        self._codex_setup_stage = "model"

    def _finish_codex_setup(self) -> None:
        self._reset_codex_setup_state()
        self.codex_mode = True
        self._reset_codex_card_state()

    def _codex_session_summary(self) -> str:
        permission = self._codex_permission or "(未设置)"
        return f"model: `{self._codex_model_display}`\npermission: `{permission}`"

    def _build_codex_exec_command(self, prompt: str) -> str:
        command = ["codex", "exec", "--json", "--color", "never", "--skip-git-repo-check"]
        if self._codex_model:
            command.extend(["--model", self._codex_model])
        if self._codex_permission:
            command.extend(["-s", self._codex_permission])
        command.append(prompt)
        return " ".join(shlex.quote(part) for part in command)

    def _handle_pending_codex_setup(self, chat_id: str, message_type: str, normalized_text: str) -> bool:
        if not self._codex_setup_stage:
            return False
        if message_type == "image":
            self._send_text(chat_id, "当前正在进入 Codex。请先用文字回复 model 或 permission；发送 /exitcodex 可取消。")
            return True
        if normalized_text.startswith("/"):
            self._send_text(chat_id, "当前正在进入 Codex。请先完成 model / permission 设置；发送 /exitcodex 可取消。")
            return True
        if self._codex_setup_stage == "model":
            model_arg, model_display = self._normalize_codex_model(normalized_text)
            if not model_display:
                self._send_text(chat_id, "model 不能为空。请回复具体模型名，或回复 `default` 使用本地默认模型。")
                return True
            self._pending_codex_model = model_arg
            self._pending_codex_model_display = model_display
            self._codex_setup_stage = "permission"
            self._send_text(
                chat_id,
                f"已记录 model：`{model_display}`\n{self._codex_permission_prompt()}",
            )
            return True
        permission = self._normalize_codex_permission(normalized_text)
        if not permission:
            self._send_text(chat_id, f"permission 无效。\n{self._codex_permission_prompt()}")
            return True
        self._codex_model = self._pending_codex_model
        self._codex_model_display = self._pending_codex_model_display or "default"
        self._codex_permission = permission
        self._finish_codex_setup()
        self._send_text(
            chat_id,
            "已进入 Codex 对话模式。\n"
            f"{self._codex_session_summary()}\n"
            "后续每条自然语言消息会使用各自的任务卡片。",
        )
        return True

    def _client_status_text(self, chat_id: str) -> str:
        active_task = self._active_task.task_id if self._active_task else "(无)"
        math_problem_id = self._math_problem.problem_id if self._math_problem else "(无)"
        selected_here = "是" if self._is_selected_chat(chat_id) else "否"
        selected_chat = self.state.selected_chat_id or "(未选择)"
        bound_chat = self.state.bound_chat_id or "(未绑定)"
        codex_setup = self._codex_setup_stage or "(无)"
        codex_permission = self._codex_permission or "(未设置)"
        return (
            f"客户端: {self.client_name}\n"
            f"client_id: `{self.client_id}`\n"
            f"当前会话已选中: {selected_here}\n"
            f"已选择 chat_id: {selected_chat}\n"
            f"绑定 chat_id: {bound_chat}\n"
            f"模式: {self._current_mode_label()}\n"
            f"codex_setup: {codex_setup}\n"
            f"codex_model: `{self._codex_model_display}`\n"
            f"codex_permission: `{codex_permission}`\n"
            f"codex_mode: {self.codex_mode}\n"
            f"math_tutor_mode: {self.math_tutor_mode}\n"
            f"active_task: {active_task}\n"
            f"math_problem: {math_problem_id}\n"
            f"选择命令: `/use {self.client_id}`"
        )

    def _release_chat_selection(self, chat_id: str, reason: str) -> bool:
        if not self._is_selected_chat(chat_id):
            return False

        codex_task = self._active_task
        if codex_task:
            codex_task.status = "interrupted"
            codex_task.finished_at = datetime.now().isoformat(timespec="seconds")
            self._append_status(reason)
            self.history.update_task(
                codex_task.task_id,
                lambda d: (d.__setitem__("status", "interrupted"), d.__setitem__("finished_at", datetime.now().isoformat(timespec="seconds"))),
            )
            self._flush_typewriter()
            self._update_task_card(force=True)

        math_problem = self._math_problem
        math_interrupted = self._interrupt_math_tutor()
        if math_problem and math_problem.card_message_id:
            self._append_math_status(reason)
            if math_interrupted:
                self._append_math_status("本机已停止当前讲解。")
            self._flush_math_typewriter()
            self._update_math_card(force=True)

        self.shell.send_ctrl_c()
        self.state.clear_selected_chat(chat_id)
        self.codex_mode = False
        self._reset_codex_setup_state()
        self._reset_codex_session_options()
        self.math_tutor_mode = False
        self._mobile_line_carry = ""
        self._active_task = None
        self._reset_codex_card_state()
        self._finalize_math_problem(sync_doc=not math_interrupted)
        return True

    def _on_receive_message(self, data: P2ImMessageReceiveV1) -> None:
        event = data.event
        if not event or not event.message or not event.sender:
            return

        if event.sender.sender_type != "user":
            return

        sender_open_id = None
        if event.sender.sender_id:
            sender_open_id = event.sender.sender_id.open_id

        chat_id = event.message.chat_id or ""
        message_id = event.message.message_id or ""
        message_type = event.message.message_type or ""
        text = self._extract_text(message_type, event.message.content)
        normalized_text = self._normalize_incoming_text(text)
        image_key = self._extract_image_key(message_type, event.message.content)
        self._log(
            "[bridge] incoming: "
            f"chat_id={chat_id} "
            f"sender_open_id={sender_open_id} "
            f"type={message_type} "
            f"text={text!r} "
            f"normalized={normalized_text!r} "
            f"image_key={image_key!r}"
        )
        if not normalized_text and not image_key:
            return

        if normalized_text.startswith("/bind"):
            if not sender_open_id or not chat_id:
                return
            self.state.bind(sender_open_id, chat_id)
            self._send_text(
                chat_id,
                (
                    f"绑定成功。\n客户端: {self.client_name}\nclient_id: `{self.client_id}`\n"
                    "说明: 多客户端场景下，请先发送 `/clients` 查看在线客户端，再发送 `/use <client_id>` 选择当前处理客户端。"
                ),
            )
            return

        if not self.state.is_authorized(sender_open_id):
            self._send_text(chat_id, "你没有权限。请先使用绑定账号发送 /bind。")
            return

        if normalized_text.startswith("/help"):
            self._send_text(chat_id, self._help_text())
            return

        if normalized_text.startswith("/clients"):
            self._send_text(chat_id, self._client_status_text(chat_id))
            return

        if normalized_text.startswith("/use"):
            if not self.state.bound_chat_id:
                self._send_text(chat_id, "当前还没有绑定会话。请先发送 /bind。")
                return
            if self.state.bound_chat_id != chat_id:
                self._send_text(chat_id, "当前会话还不是绑定会话。请先在这个会话里重新发送 /bind。")
                return
            parts = normalized_text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                self._send_text(chat_id, "用法: /use <client_id>")
                return
            target_client_id = parts[1].strip()
            if self.client_id == target_client_id:
                already_selected = self._is_selected_chat(chat_id)
                self.state.select_chat(chat_id)
                if already_selected:
                    self._send_text(chat_id, f"当前会话已由客户端 {self._client_label()} 处理。")
                else:
                    self._send_text(chat_id, f"已选择客户端 {self._client_label()}。后续普通命令将由本机处理。")
                return
            if self._release_chat_selection(chat_id, "当前会话已切换到其他客户端，本机停止处理。"):
                self._send_text(chat_id, f"客户端 {self._client_label()} 已释放当前会话。")
            return

        if normalized_text.startswith("/leaveclient") or normalized_text.startswith("/unuse"):
            if self._release_chat_selection(chat_id, "当前会话已取消客户端选择，本机停止处理。"):
                self._send_text(chat_id, f"客户端 {self._client_label()} 已退出当前会话。")
            return

        if normalized_text.startswith("/status"):
            bound = self.state.bound_chat_id or "(未绑定)"
            selected_chat = self.state.selected_chat_id or "(未选择)"
            selected_here = "是" if self._is_selected_chat(chat_id) else "否"
            codex_setup = self._codex_setup_stage or "(无)"
            codex_permission = self._codex_permission or "(未设置)"
            active_task = self._active_task.task_id if self._active_task else "(无)"
            card_id = self._session_card_message_id or "(无)"
            general_card_id = self._general_card_message_id or "(无)"
            math_problem_id = self._math_problem.problem_id if self._math_problem else "(无)"
            math_card_id = self._math_problem.card_message_id if self._math_problem else "(无)"
            self._send_text(
                chat_id,
                (
                    f"bridge 运行中\n客户端: {self.client_name}\nclient_id: `{self.client_id}`\n当前会话已选中: {selected_here}\n"
                    f"已选择 chat_id: {selected_chat}\n绑定 chat_id: {bound}\nshell: {self.config.shell_path}\n"
                    f"codex_setup: {codex_setup}\ncodex_model: `{self._codex_model_display}`\ncodex_permission: `{codex_permission}`\n"
                    f"codex_mode: {self.codex_mode}\nmath_tutor_mode: {self.math_tutor_mode}\nactive_task: {active_task}\n"
                    f"math_problem: {math_problem_id}\ngeneral_card: {general_card_id}\ncodex_card: {card_id}\nmath_card: {math_card_id}"
                ),
            )
            return

        if normalized_text.startswith("/ps"):
            bound = self.state.bound_chat_id or "(未绑定)"
            selected_chat = self.state.selected_chat_id or "(未选择)"
            selected_here = "是" if self._is_selected_chat(chat_id) else "否"
            codex_setup = self._codex_setup_stage or "(无)"
            codex_permission = self._codex_permission or "(未设置)"
            active_task = self._active_task.task_id if self._active_task else "(无)"
            card_id = self._session_card_message_id or "(无)"
            general_card_id = self._general_card_message_id or "(无)"
            math_problem_id = self._math_problem.problem_id if self._math_problem else "(无)"
            math_card_id = self._math_problem.card_message_id if self._math_problem else "(无)"
            self._send_text(
                chat_id,
                (
                    f"bridge 运行中\n客户端: {self.client_name}\nclient_id: `{self.client_id}`\n当前会话已选中: {selected_here}\n"
                    f"已选择 chat_id: {selected_chat}\n绑定 chat_id: {bound}\nshell: {self.config.shell_path}\n"
                    f"codex_setup: {codex_setup}\ncodex_model: `{self._codex_model_display}`\ncodex_permission: `{codex_permission}`\n"
                    f"codex_mode: {self.codex_mode}\nmath_tutor_mode: {self.math_tutor_mode}\nactive_task: {active_task}\n"
                    f"math_problem: {math_problem_id}\ngeneral_card: {general_card_id}\ncodex_card: {card_id}\nmath_card: {math_card_id}"
                ),
            )
            return

        if self._handle_pending_codex_setup(chat_id, message_type, normalized_text):
            return

        if not self._selected_required(chat_id, normalized_text, message_type):
            return

        if normalized_text.startswith("/history"):
            self._handle_history_command(chat_id, normalized_text)
            return

        if normalized_text.startswith("/ctrlc"):
            self.shell.send_ctrl_c()
            math_interrupted = self._interrupt_math_tutor()
            if self._active_task:
                self._active_task.status = "interrupted"
                self._append_status("本轮请求被 Ctrl+C 中断。")
                self.history.update_task(
                    self._active_task.task_id,
                    lambda d: (d.__setitem__("status", "interrupted"), d.__setitem__("finished_at", datetime.now().isoformat(timespec="seconds"))),
                )
                self._flush_typewriter()
                self._update_task_card(force=True)
            self._mobile_line_carry = ""
            if self.codex_mode and self._session_card_message_id:
                self._append_status("已发送 Ctrl+C。")
                self._flush_typewriter()
                self._update_task_card(force=True)
            if math_interrupted and self._math_problem and self._math_problem.card_message_id:
                self._append_math_status("已发送 Ctrl+C。")
                self._flush_math_typewriter()
                self._update_math_card(force=True)
            else:
                if not self._active_task and not math_interrupted:
                    self._send_text(chat_id, "已发送 Ctrl+C")
            return

        if normalized_text == "设置数学辅导提示词":
            self._send_text(chat_id, "用法：设置数学辅导提示词 你的提示词")
            return

        if normalized_text.startswith("设置数学辅导提示词 "):
            raw_prompt = text[len("设置数学辅导提示词"):].strip() if text.startswith("设置数学辅导提示词") else ""
            prompt = raw_prompt or normalized_text[len("设置数学辅导提示词 "):].strip()
            if not prompt:
                self._send_text(chat_id, "用法：设置数学辅导提示词 你的提示词")
                return
            self.state.set_math_tutor_system_prompt(prompt)
            self._send_text(chat_id, "数学辅导提示词已更新。")
            return

        if normalized_text == "查看数学辅导提示词":
            self._send_text(chat_id, self._math_system_prompt())
            return

        if normalized_text == "清空数学辅导提示词":
            self.state.set_math_tutor_system_prompt("")
            self._send_text(chat_id, "数学辅导提示词已恢复为默认设置。")
            return

        if normalized_text.startswith("创建数学总结文档"):
            raw_title = text[len("创建数学总结文档"):].strip() if text.startswith("创建数学总结文档") else ""
            title = raw_title or normalized_text[len("创建数学总结文档"):].strip() or self.config.math_tutor_doc_title
            document_id = self._create_math_summary_document(title)
            if document_id:
                self._send_text(chat_id, f"数学总结文档已创建：{document_id}\n{self._math_summary_doc_link(document_id)}")
            else:
                self._send_text(chat_id, self._last_math_doc_error or "创建数学总结文档失败，请检查飞书文档权限。")
            return

        if normalized_text.startswith("绑定数学总结文档 "):
            raw = normalized_text[len("绑定数学总结文档 "):].strip()
            document_id = self._extract_document_id(raw)
            if not document_id:
                self._send_text(chat_id, "未识别到文档 ID。可直接发送 document_id，或发送包含 /docx/<id> 的链接。")
                return
            self.state.set_math_summary_doc(document_id)
            self._send_text(chat_id, f"已绑定数学总结文档：{document_id}\n{self._math_summary_doc_link(document_id)}")
            return

        if normalized_text == "查看数学总结文档":
            if self.state.math_summary_doc_id:
                self._send_text(
                    chat_id,
                    f"当前数学总结文档：{self.state.math_summary_doc_id}\n{self._math_summary_doc_link(self.state.math_summary_doc_id)}",
                )
            else:
                self._send_text(chat_id, "当前未绑定数学总结文档。可发送“创建数学总结文档”自动创建。")
            return

        if normalized_text == "关闭数学总结文档":
            self.state.clear_math_summary_doc()
            self._send_text(chat_id, "已关闭数学总结文档同步。")
            return

        if normalized_text == "教我做题":
            if self.codex_mode or self._codex_setup_stage:
                self._send_text(chat_id, "当前在 Codex 对话模式。请先发送 /exitcodex，再进入数学辅导。")
                return
            if self.math_tutor_mode:
                self._send_text(chat_id, "已在数学辅导模式。直接发送题目文字或图片即可。")
                return
            self.math_tutor_mode = True
            self._send_text(chat_id, "已进入数学辅导模式。请发送题目文字或图片；发送“下一题”会开启新卡片。", force_new=True)
            return

        if normalized_text in {"退出做题", "结束做题", "退出数学辅导"}:
            if not self.math_tutor_mode:
                self._send_text(chat_id, "当前不在数学辅导模式。")
                return
            if self._is_math_running():
                self._send_text(chat_id, "当前这道题仍在讲解中。请稍后，或先发送 /ctrlc。")
                return
            self._finalize_math_problem(sync_doc=True)
            self.math_tutor_mode = False
            self._send_text(chat_id, "已退出数学辅导模式。", force_new=True)
            return

        if normalized_text == "下一题":
            if not self.math_tutor_mode:
                self._send_text(chat_id, "请先发送“教我做题”进入数学辅导模式。")
                return
            if self._is_math_running():
                self._send_text(chat_id, "当前这道题仍在讲解中。请稍后，或先发送 /ctrlc。")
                return
            self._finalize_math_problem(sync_doc=True)
            self._send_text(chat_id, "已切换到下一题。请发送新的题目文字或图片。", force_new=True)
            return

        if normalized_text.startswith("/codex"):
            if not self._command_allowed("codex exec"):
                self._send_text(chat_id, "命令被策略拒绝。")
                return
            if self.math_tutor_mode:
                self._send_text(chat_id, "当前在数学辅导模式。请先发送“退出做题”，再进入 Codex 对话模式。")
                return
            if self._codex_setup_stage:
                if self._codex_setup_stage == "model":
                    self._send_text(chat_id, "正在等待你回复 model。回复 `default` 使用本地默认模型，或发送 /exitcodex 取消。")
                else:
                    self._send_text(chat_id, f"已记录 model：`{self._pending_codex_model_display or 'default'}`\n{self._codex_permission_prompt()}")
                return
            if self.codex_mode:
                if self._active_task and self._active_task.status == "running":
                    self._send_text(chat_id, "已在 Codex 对话模式，当前任务仍在执行。")
                else:
                    self._send_text(chat_id, "已在 Codex 对话模式。\n" f"{self._codex_session_summary()}\n直接发送自然语言消息即可创建新的任务卡片。")
                return
            self._begin_codex_setup()
            self._send_text(
                chat_id,
                "即将进入 Codex 对话模式。\n请先回复 model；如使用本地默认模型，请回复 `default`。",
                force_new=True,
            )
            return

        if normalized_text.startswith("/exitcodex"):
            if self._active_task:
                self._active_task.status = "interrupted"
                self._append_status("会话退出前中断当前请求。")
                self.history.update_task(
                    self._active_task.task_id,
                    lambda d: (d.__setitem__("status", "interrupted"), d.__setitem__("finished_at", datetime.now().isoformat(timespec="seconds"))),
                )
                self._flush_typewriter()
                self._update_task_card(force=True)
            was_pending = bool(self._codex_setup_stage) and not self.codex_mode
            self.codex_mode = False
            self._reset_codex_setup_state()
            self._reset_codex_session_options()
            self._mobile_line_carry = ""
            self._active_task = None
            self._reset_codex_card_state()
            self.shell.send_ctrl_c()
            if was_pending:
                self._send_text(chat_id, "已取消进入 Codex 对话模式。", force_new=True)
            else:
                self._send_text(chat_id, "已退出 Codex 终端模式。", force_new=True)
            return

        if normalized_text.startswith("/cmd"):
            command = normalized_text[len("/cmd"):].strip()
            if not command:
                self._send_text(chat_id, "用法: /cmd <shell command>")
                return
            if not self._command_allowed(command):
                self._send_text(chat_id, "命令被策略拒绝。")
                return
            self._log(f"[bridge] exec -> shell: {command}")
            self.shell.send_command(command)
            self._send_text(chat_id, f"$ {command}")
            return

        if message_type == "image":
            if self.math_tutor_mode:
                self._handle_math_tutor_input(chat_id, sender_open_id or "", text="", image_key=image_key, message_id=message_id)
            else:
                self._send_text(chat_id, "当前仅数学辅导模式支持图片题。请先发送“教我做题”。")
            return

        if self.math_tutor_mode and normalized_text and not normalized_text.startswith("/"):
            self._handle_math_tutor_input(chat_id, sender_open_id or "", text=normalized_text, image_key="", message_id=message_id)
            return

        if self.codex_mode and not normalized_text.startswith("/"):
            if self._active_task and self._active_task.status == "running":
                self._append_status("上一条请求仍在执行，请稍后或发送 /ctrlc。")
                self._update_task_card(force=True)
                return
            self._reset_codex_card_state()
            self._session_answer_parts = ["_等待回答中..._"]
            task = self._create_task(chat_id, sender_open_id or "", normalized_text)
            self._active_task = task
            self._append_status(f"会话配置：model=`{self._codex_model_display}` permission=`{self._codex_permission}`")
            self._append_status(f"新请求：{normalized_text}")
            self._append_status("请求已接收，正在执行。")
            self._session_card_message_id = self._send_session_card(chat_id)
            task.card_message_id = self._session_card_message_id
            self.history.update_task(task.task_id, lambda d, cid=self._session_card_message_id: d.__setitem__("card_message_id", cid))
            self._update_task_card(force=True)
            command = self._build_codex_exec_command(normalized_text)
            self._log(f"[bridge] exec -> shell: {command}")
            self.shell.send_command(command)
            return

        if normalized_text.startswith("/"):
            self._send_text(chat_id, "未知命令。发送 /help 查看可用命令。")

    def _forward_terminal_output(self) -> None:
        buffer = ""
        last_flush = time.monotonic()

        while not self.shutdown.is_set():
            try:
                chunk = self.shell.output_queue.get(timeout=0.2)
                buffer += chunk
            except queue.Empty:
                pass

            now = time.monotonic()
            flush_interval = self.config.codex_flush_interval_seconds if self.codex_mode else self.config.flush_interval_seconds
            due = (now - last_flush) >= flush_interval
            if due and buffer:
                cleaned = self._clean_output(buffer)
                buffer = ""
                last_flush = now
                if cleaned.strip():
                    self._log_shell_output(cleaned)
                    if self.codex_mode:
                        self._process_codex_chunk(cleaned)
                    else:
                        mobile_text = self._format_for_mobile(cleaned)
                        if mobile_text.strip():
                            self._send_bound_chat(mobile_text)
            elif self.codex_mode and self._session_card_message_id and self._has_pending_typewriter():
                self._update_task_card()
            elif self._math_problem and self._math_problem.card_message_id and self._has_pending_math_typewriter():
                self._update_math_card()

        if buffer.strip():
            cleaned = self._clean_output(buffer)
            self._log_shell_output(cleaned)
            if self.codex_mode:
                self._process_codex_chunk(cleaned, flush_tail=True)
            else:
                mobile_text = self._format_for_mobile(cleaned, flush_tail=True)
                if mobile_text.strip():
                    self._send_bound_chat(mobile_text)

    def _clean_output(self, text: str) -> str:
        cleaned = ANSI_RE.sub("", text)
        cleaned = cleaned.replace("\r", "\n")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned

    def _log_shell_output(self, text: str) -> None:
        if not text.strip():
            return
        self._log("[bridge] shell output >>>")
        self._log(text.rstrip("\n"))
        self._log("[bridge] <<< shell output")

    def _format_for_mobile(self, text: str, flush_tail: bool = False) -> str:
        if not self.codex_mode:
            return text

        data = self._mobile_line_carry + text
        lines = data.split("\n")
        if data.endswith("\n") or flush_tail:
            self._mobile_line_carry = ""
        else:
            self._mobile_line_carry = lines.pop()

        out: list[str] = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            formatted_json = self._format_codex_json_line(line)
            if formatted_json is not None:
                if formatted_json:
                    out.append(formatted_json)
                continue

            if line == "codex":
                continue
            if line.startswith("(base) ") or line.startswith("% "):
                continue
            if line.startswith("Error:"):
                out.append(f"[错误] {line[6:].strip()}")
            else:
                out.append(line)

        return "\n".join(out).strip()

    def _format_codex_json_line(self, line: str) -> Optional[str]:
        if not (line.startswith("{") and line.endswith("}")):
            return None
        try:
            obj = json.loads(line)
        except Exception:
            return None

        event_type = str(obj.get("type") or obj.get("event") or obj.get("kind") or "").strip()
        item = obj.get("item")
        item_type = ""
        command = ""
        if isinstance(item, dict):
            item_type = str(item.get("type") or "").strip()
            command = self._to_short_text(item.get("command"))

        if event_type == "item.started" and item_type == "command_execution":
            if command:
                return f"[命令执行中] {command}"
            return "[命令执行中]"

        if event_type == "item.completed" and item_type == "command_execution":
            if command:
                return f"[执行命令] {command}"
            return ""

        if event_type == "item.completed" and item_type == "agent_message":
            return self._to_short_text(item.get("text")) if isinstance(item, dict) else ""

        return ""

    @staticmethod
    def _to_short_text(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        return ""

    def _create_task(self, chat_id: str, sender_open_id: str, prompt: str) -> CodexTask:
        task_id = self.history.create_task(chat_id, sender_open_id, prompt)
        now = datetime.now().isoformat(timespec="seconds")
        task = CodexTask(
            task_id=task_id,
            chat_id=chat_id,
            sender_open_id=sender_open_id,
            prompt=prompt,
            status="running",
            started_at=now,
            updated_at=now,
            finished_at="",
            commands=[],
            answer_parts=[],
            events=[],
            card_message_id="",
            last_card_push_ts=0.0,
        )
        self._log(f"[bridge] task created: {task.task_id} prompt={prompt!r}")
        return task

    def _append_status(self, text: str) -> None:
        if text.strip():
            self._status_pending.append(text)

    def _append_answer(self, text: str) -> None:
        if text.strip():
            self._answer_pending.append(text)

    def _reset_codex_card_state(self) -> None:
        self._session_card_message_id = ""
        self._session_status_parts = []
        self._session_answer_parts = []
        self._status_pending = []
        self._answer_pending = []
        self._status_typing_current = ""
        self._answer_typing_current = ""
        self._status_typing_index = 0
        self._answer_typing_index = 0

    def _reset_general_card_state(self) -> None:
        self._general_card_message_id = ""
        self._general_feed_parts = []

    def _has_pending_typewriter(self) -> bool:
        return bool(
            self._status_pending
            or self._answer_pending
            or self._status_typing_current
            or self._answer_typing_current
        )

    def _flush_typewriter(self) -> bool:
        progressed = False
        while self._advance_typewriter():
            progressed = True
        return progressed

    def _advance_typewriter(self) -> bool:
        progressed = False
        progressed |= self._advance_status_typewriter()
        progressed |= self._advance_answer_typewriter()
        return progressed

    def _advance_status_typewriter(self) -> bool:
        if not self._status_typing_current and self._status_pending:
            self._status_typing_current = self._status_pending.pop(0)
            self._status_typing_index = 0
            self._session_status_parts.append("")
        if not self._status_typing_current:
            return False
        step = max(1, self.config.typewriter_status_chars_per_tick)
        end = min(len(self._status_typing_current), self._status_typing_index + step)
        self._session_status_parts[-1] = self._status_typing_current[:end]
        self._status_typing_index = end
        if end >= len(self._status_typing_current):
            self._status_typing_current = ""
            self._status_typing_index = 0
            if len(self._session_status_parts) > 500:
                self._session_status_parts = self._session_status_parts[-500:]
        return True

    def _advance_answer_typewriter(self) -> bool:
        if not self._answer_typing_current and self._answer_pending:
            self._answer_typing_current = self._answer_pending.pop(0)
            self._answer_typing_index = 0
            if self._session_answer_parts == ["_等待回答中..._"]:
                self._session_answer_parts = []
            self._session_answer_parts.append("")
        if not self._answer_typing_current:
            return False
        step = max(1, self.config.typewriter_answer_chars_per_tick)
        end = min(len(self._answer_typing_current), self._answer_typing_index + step)
        self._session_answer_parts[-1] = self._answer_typing_current[:end]
        self._answer_typing_index = end
        if end >= len(self._answer_typing_current):
            self._answer_typing_current = ""
            self._answer_typing_index = 0
            if len(self._session_answer_parts) > 300:
                self._session_answer_parts = self._session_answer_parts[-300:]
        return True

    @staticmethod
    def _truncate_inline_text(text: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", (text or "").strip())
        if len(compact) <= limit:
            return compact
        return compact[: max(1, limit - 3)] + "..."

    @staticmethod
    def _escape_card_literal(text: str) -> str:
        escaped = (text or "").replace("&", "&amp;")
        replacements = {
            ">": "&#62;",
            "<": "&#60;",
            "~": "&sim;",
            "-": "&#45;",
            "!": "&#33;",
            "*": "&#42;",
            "/": "&#47;",
            "\\": "&#92;",
            "[": "&#91;",
            "]": "&#93;",
            "(": "&#40;",
            ")": "&#41;",
            "#": "&#35;",
            ":": "&#58;",
            "+": "&#43;",
            '"': "&#34;",
            "'": "&#39;",
            "`": "&#96;",
            "$": "&#36;",
            "_": "&#95;",
        }
        for src, dst in replacements.items():
            escaped = escaped.replace(src, dst)
        return escaped

    @staticmethod
    def _markdown_element(content: str, text_size: str = "normal", margin: str = "0px") -> dict:
        return {
            "tag": "markdown",
            "content": content or " ",
            "text_size": text_size,
            "margin": margin,
        }

    @classmethod
    def _split_large_markdown_block(cls, block: str, max_chars: int) -> list[str]:
        if len(block) <= max_chars:
            return [block]

        stripped = block.strip("\n")
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.split("\n")
            if len(lines) >= 2:
                opening = lines[0]
                closing = lines[-1]
                body_lines = lines[1:-1] or [""]
                chunks: list[str] = []
                current: list[str] = []
                overhead = len(opening) + len(closing) + 2
                for line in body_lines:
                    candidate = "\n".join(current + [line])
                    if current and (len(candidate) + overhead) > max_chars:
                        chunks.append(f"{opening}\n" + "\n".join(current) + f"\n{closing}")
                        current = [line]
                    else:
                        current.append(line)
                if current:
                    chunks.append(f"{opening}\n" + "\n".join(current) + f"\n{closing}")
                return chunks

        chunks: list[str] = []
        current = ""
        for line in block.split("\n"):
            candidate = line if not current else current + "\n" + line
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [block[:max_chars]]

    @classmethod
    def _split_markdown_for_card(
        cls,
        text: str,
        max_chars: int = CARD_MARKDOWN_CHUNK_CHARS,
        max_chunks: int = CARD_MAX_MARKDOWN_CHUNKS,
    ) -> list[str]:
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        blocks: list[str] = []
        current: list[str] = []
        fence_open = False
        for line in normalized.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("```"):
                if not fence_open:
                    if current:
                        blocks.append("\n".join(current).strip("\n"))
                        current = []
                    current = [line]
                    fence_open = True
                else:
                    current.append(line)
                    blocks.append("\n".join(current).strip("\n"))
                    current = []
                    fence_open = False
                continue
            if fence_open:
                current.append(line)
                continue
            if not line.strip():
                if current:
                    blocks.append("\n".join(current).strip("\n"))
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append("\n".join(current).strip("\n"))

        chunks: list[str] = []
        current_chunk = ""
        for block in blocks:
            if len(block) > max_chars:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                chunks.extend(cls._split_large_markdown_block(block, max_chars))
                continue

            candidate = block if not current_chunk else current_chunk + "\n\n" + block
            if current_chunk and len(candidate) > max_chars:
                chunks.append(current_chunk.strip())
                current_chunk = block
            else:
                current_chunk = candidate
        if current_chunk:
            chunks.append(current_chunk.strip())

        if len(chunks) > max_chunks:
            kept = chunks[: max_chunks - 1]
            kept.append("_内容过长，已折叠更早片段。_")
            return kept
        return chunks

    @classmethod
    def _wrap_code_fence(cls, text: str, language: str = "plain_text") -> str:
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not body:
            body = " "
        fence = "```"
        while fence in body:
            fence += "`"
        return f"{fence}{language}\n{body}\n{fence}"

    @staticmethod
    def _status_theme(status: str) -> tuple[str, str]:
        key = (status or "").strip().lower()
        if key == "completed":
            return "green", "green"
        if key == "interrupted":
            return "orange", "orange"
        if key in {"failed", "error"}:
            return "red", "red"
        if key == "running":
            return "blue", "blue"
        return "grey", "neutral"

    @staticmethod
    def _header_tag(text: str, color: str) -> dict:
        return {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": text},
            "color": color,
        }

    def _build_rich_card(
        self,
        *,
        title: str,
        subtitle: str,
        template: str,
        summary: str,
        tags: list[dict],
        elements: list[dict],
    ) -> dict:
        body_elements = elements[:CARD_MAX_BODY_ELEMENTS]
        return {
            "schema": "2.0",
            "config": {
                "enable_forward": True,
                "update_multi": True,
                "width_mode": "fill",
                "summary": {"content": summary or "OpenCodex"},
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text", "content": subtitle or " "},
                "template": template,
                "text_tag_list": tags[:3],
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "vertical_spacing": "8px",
                "elements": body_elements or [self._markdown_element("_暂无内容_")],
            },
        }

    def _build_session_card_json(self) -> dict:
        task = self._active_task
        status_md, answer_md = self._render_session_sections()
        prompt = task.prompt if task else ""
        status = task.status if task else "running"
        template, tag_color = self._status_theme(status)
        commands = task.commands if task else []
        recent_commands = commands[-CARD_MAX_COMMAND_BLOCKS:]
        elements = [
            self._markdown_element("### Prompt", text_size="heading"),
            self._markdown_element(self._wrap_code_fence(prompt, "plain_text")),
            self._markdown_element("<hr>"),
            self._markdown_element("### Activity", text_size="heading"),
            self._markdown_element(status_md),
        ]
        if recent_commands:
            dropped = max(0, len(commands) - len(recent_commands))
            elements.extend(
                [
                    self._markdown_element("<hr>"),
                    self._markdown_element("### Commands", text_size="heading"),
                ]
            )
            if dropped > 0:
                elements.append(self._markdown_element(f"_已折叠更早命令 {dropped} 条（可用 /history 查看完整）_"))
            base_index = len(commands) - len(recent_commands) + 1
            for idx, command in enumerate(recent_commands, start=base_index):
                elements.append(self._markdown_element(f"**#{idx}**"))
                elements.append(self._markdown_element(self._wrap_code_fence(self._format_status_command(command), "shell")))
        elements.extend(
            [
                self._markdown_element("<hr>"),
                self._markdown_element("### Response", text_size="heading"),
            ]
        )
        answer_chunks = self._split_markdown_for_card(answer_md) or ["_等待回答中..._"]
        elements.extend(self._markdown_element(chunk) for chunk in answer_chunks)
        title = "Codex CLI"
        subtitle = self._truncate_inline_text(prompt, CARD_HEADER_TEXT_LIMIT) or "等待请求"
        summary = self._truncate_inline_text(answer_md if answer_md and answer_md != "_等待回答中..._" else prompt, CARD_SUMMARY_TEXT_LIMIT)
        tags = [
            self._header_tag("Codex", "blue"),
            self._header_tag(status.upper(), tag_color),
        ]
        return self._build_rich_card(
            title=title,
            subtitle=subtitle,
            template=template,
            summary=summary or "Codex 会话",
            tags=tags,
            elements=elements,
        )

    def _build_math_card_json(self) -> dict:
        problem = self._math_problem
        status_md, answer_md = self._render_math_sections()
        status = problem.status if problem else "running"
        template, tag_color = self._status_theme(status)
        title = "数学辅导"
        subtitle = f"第 {problem.problem_index} 题" if problem else "数学辅导"
        tags = [
            self._header_tag("Math", "indigo"),
            self._header_tag(status.upper(), tag_color),
        ]
        elements = [
            self._markdown_element("### Activity", text_size="heading"),
            self._markdown_element(status_md),
            self._markdown_element("<hr>"),
            self._markdown_element("### Explanation", text_size="heading"),
        ]
        answer_chunks = self._split_markdown_for_card(answer_md) or ["_等待讲解中..._"]
        elements.extend(self._markdown_element(chunk) for chunk in answer_chunks)
        summary = self._truncate_inline_text(answer_md if answer_md and answer_md != "_等待讲解中..._" else subtitle, CARD_SUMMARY_TEXT_LIMIT)
        return self._build_rich_card(
            title=title,
            subtitle=subtitle,
            template=template,
            summary=summary or "数学辅导",
            tags=tags,
            elements=elements,
        )

    def _build_general_card_json(self) -> dict:
        status_md, answer_md = self._render_general_sections()
        if self.math_tutor_mode:
            mode_text = "数学辅导中"
            template = "indigo"
            mode_color = "indigo"
        elif self.codex_mode:
            mode_text = "Codex 对话中"
            template = "blue"
            mode_color = "blue"
        else:
            mode_text = "普通终端模式"
            template = "grey"
            mode_color = "neutral"
        elements = [
            self._markdown_element("### System", text_size="heading"),
            self._markdown_element(status_md),
            self._markdown_element("<hr>"),
            self._markdown_element("### Feed", text_size="heading"),
        ]
        answer_chunks = self._split_markdown_for_card(answer_md) or ["_暂无消息_"]
        elements.extend(self._markdown_element(chunk) for chunk in answer_chunks)
        summary = self._truncate_inline_text(answer_md, CARD_SUMMARY_TEXT_LIMIT) or "系统消息"
        return self._build_rich_card(
            title="OpenCodex",
            subtitle=mode_text,
            template=template,
            summary=summary,
            tags=[self._header_tag(mode_text, mode_color)],
            elements=elements,
        )

    def _resolve_template_id(self, kind: str) -> str:
        if kind == "general":
            return self.config.general_card_template_id or self.config.card_template_id
        if kind == "codex":
            return self.config.codex_card_template_id or self.config.card_template_id
        if kind == "math":
            return self.config.math_card_template_id or self.config.card_template_id
        return self.config.card_template_id

    def _render_session_sections(self) -> tuple[str, str]:
        status_source = self._session_status_parts[-max(1, self.config.card_status_max_lines):]
        status_lines = []
        for idx, item in enumerate(status_source, start=1):
            safe_item = self._escape_card_literal(item)
            status_lines.append(f"{idx}. {safe_item}")
        dropped_status = max(0, len(self._session_status_parts) - len(status_source))
        if dropped_status > 0:
            status_lines.insert(0, f"_已折叠更早状态 {dropped_status} 条（可用 /history 查看完整）_")
        if not status_lines:
            status_lines.append("- 暂无状态")

        answer = "\n\n".join([p for p in self._session_answer_parts if p.strip()]).strip()
        if not answer:
            answer = "_等待回答中..._"

        return "\n".join(status_lines), answer

    def _send_session_card(self, chat_id: str) -> str:
        template_id = self._resolve_template_id("codex")
        if template_id:
            status_md, answer_md = self._render_session_sections()
            msg_id = self._send_template_card(
                chat_id,
                template_id=template_id,
                raw_text="",
                status_text=status_md,
                answer_text=answer_md,
            )
            if msg_id:
                return msg_id
        card = self._build_session_card_json()
        msg_id = self._send_interactive_card(chat_id, card)
        if msg_id:
            return msg_id
        status_md, answer_md = self._render_session_sections()
        return self._send_template_card(
            chat_id,
            template_id=self.config.card_template_id,
            raw_text="",
            status_text=status_md,
            answer_text=answer_md,
        )

    def _update_task_card(self, force: bool = False) -> None:
        if not self._session_card_message_id:
            return
        if self._active_task and not self._is_selected_chat(self._active_task.chat_id):
            return
        progressed = self._advance_typewriter()
        now = time.monotonic()
        if self._active_task and not force and (now - self._active_task.last_card_push_ts) < self.config.card_update_interval_seconds:
            return
        if not force and not progressed:
            return
        template_id = self._resolve_template_id("codex")
        if template_id:
            status_md, answer_md = self._render_session_sections()
            if self._patch_card_message(
                self._session_card_message_id,
                template_id=template_id,
                raw_text="",
                status_text=status_md,
                answer_text=answer_md,
            ):
                if self._active_task:
                    self._active_task.last_card_push_ts = now
                    self.history.update_task(self._active_task.task_id, lambda d: d.__setitem__("updated_at", datetime.now().isoformat(timespec="seconds")))
                    self._log(f"[bridge] card updated message_id={self._session_card_message_id} task_id={self._active_task.task_id}")
                else:
                    self._log(f"[bridge] card updated message_id={self._session_card_message_id}")
                return
        card = self._build_session_card_json()
        if self._patch_interactive_card(self._session_card_message_id, card):
            if self._active_task:
                self._active_task.last_card_push_ts = now
                self.history.update_task(self._active_task.task_id, lambda d: d.__setitem__("updated_at", datetime.now().isoformat(timespec="seconds")))
                self._log(f"[bridge] card updated message_id={self._session_card_message_id} task_id={self._active_task.task_id}")
            else:
                self._log(f"[bridge] card updated message_id={self._session_card_message_id}")
            return
        self._log(
            f"[bridge] card update failed task_id={self._active_task.task_id if self._active_task else '-'} message_id={self._session_card_message_id}"
        )

    def _process_codex_chunk(self, text: str, flush_tail: bool = False) -> None:
        data = self._mobile_line_carry + text
        lines = data.split("\n")
        if data.endswith("\n") or flush_tail:
            self._mobile_line_carry = ""
        else:
            self._mobile_line_carry = lines.pop()

        task = self._active_task
        if not task:
            return

        changed = False
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            event = self._parse_codex_event_line(line)
            if not event:
                if line.startswith("Error:"):
                    msg = line[6:].strip()
                    task.events.append(f"error:{msg}")
                    self.history.update_task(task.task_id, lambda d, m=msg: d["events"].append(f"error:{m}"))
                    changed = True
                continue

            kind = event.get("kind")
            value = event.get("value", "")
            if kind == "command_started" and value:
                task.commands.append(value)
                status_cmd = self._format_status_command(value)
                self._append_status(f"命令执行中：`{status_cmd}`")
                self._log(f"[bridge] task command_started task_id={task.task_id} command={value}")
                self.history.update_task(
                    task.task_id,
                    lambda d, v=value: (d["commands"].append(v), d["events"].append(f"command_started:{v}")),
                )
                changed = True
            elif kind == "agent_message" and value:
                task.answer_parts.append(value)
                self._append_answer(value)
                self._log(f"[bridge] task agent_message task_id={task.task_id} size={len(value)}")
                self.history.update_task(
                    task.task_id,
                    lambda d, v=value: (d["answer_parts"].append(v), d["events"].append("agent_message")),
                )
                changed = True
            elif kind == "completed":
                task.status = "completed"
                task.finished_at = datetime.now().isoformat(timespec="seconds")
                self._append_status("本轮请求执行完成。")
                self.history.update_task(
                    task.task_id,
                    lambda d: (d.__setitem__("status", "completed"), d.__setitem__("finished_at", datetime.now().isoformat(timespec="seconds"))),
                )
                changed = True

        if changed:
            self._update_task_card()
        if task.status == "completed":
            self._flush_typewriter()
            self._update_task_card(force=True)
            self._active_task = None

    def _parse_codex_event_line(self, line: str) -> Optional[dict]:
        if not (line.startswith("{") and line.endswith("}")):
            return None
        try:
            obj = json.loads(line)
        except Exception:
            return None
        event_type = str(obj.get("type") or "")
        if event_type == "thread.started":
            return {"kind": "thread_started", "value": str(obj.get("thread_id") or "").strip()}
        if event_type == "turn.completed":
            return {"kind": "completed", "value": ""}
        item = obj.get("item")
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "")
        if event_type == "item.started" and item_type == "command_execution":
            return {"kind": "command_started", "value": str(item.get("command") or "").strip()}
        if event_type == "item.completed" and item_type == "agent_message":
            return {"kind": "agent_message", "value": str(item.get("text") or "").strip()}
        return None

    @staticmethod
    def _format_status_command(command: str) -> str:
        cmd = command.strip().replace("\n", " ")
        m = re.match(r"^/bin/(?:zsh|bash)\s+-lc\s+'(.*)'$", cmd)
        if m:
            cmd = m.group(1)
        if len(cmd) > 180:
            cmd = cmd[:180] + "..."
        return cmd

    def _handle_history_command(self, chat_id: str, normalized_text: str) -> None:
        parts = normalized_text.split()
        if len(parts) == 1:
            items = self.history.list_recent(10)
            if not items:
                self._send_text(chat_id, "暂无历史记录。")
                return
            lines = ["最近历史任务："]
            for it in items:
                prompt = it["prompt"][:40] + ("..." if len(it["prompt"]) > 40 else "")
                lines.append(f"- `{it['task_id']}` [{it['status']}] {prompt}")
            lines.append("用法：/history <task_id> 查看详情，/history clear 清空")
            self._send_text(chat_id, "\n".join(lines))
            return
        if len(parts) == 2 and parts[1].lower() == "clear":
            count = self.history.clear()
            self._send_text(chat_id, f"历史记录已清空，共删除 {count} 条。")
            return
        task_id = parts[1]
        data = self.history.get_task(task_id)
        if not data:
            self._send_text(chat_id, f"未找到任务：{task_id}")
            return
        commands = data.get("commands", [])
        answer = "\n\n".join(data.get("answer_parts", [])) or "(空)"
        body = (
            f"任务ID: `{task_id}`\n"
            f"状态: {data.get('status')}\n"
            f"开始: {data.get('started_at')}\n"
            f"结束: {data.get('finished_at') or '(进行中)'}\n"
            f"请求: {data.get('prompt')}\n\n"
            f"命令数: {len(commands)}\n"
            f"回答:\n{answer}"
        )
        self._send_text(chat_id, body)

    def _create_math_problem(self, chat_id: str, sender_open_id: str) -> MathTutorProblem:
        self._math_problem_counter += 1
        now = datetime.now().isoformat(timespec="seconds")
        problem = MathTutorProblem(
            problem_id=datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8],
            problem_index=self._math_problem_counter,
            chat_id=chat_id,
            sender_open_id=sender_open_id,
            status="idle",
            started_at=now,
            updated_at=now,
            finished_at="",
            thread_id="",
            card_message_id="",
            last_card_push_ts=0.0,
            user_inputs=[],
            answer_parts=[],
            image_paths=[],
            doc_synced=False,
            doc_document_id="",
            doc_start_index=-1,
            doc_block_count=0,
        )
        self._math_problem = problem
        self._reset_math_card_state()
        self._math_answer_parts = ["_等待讲解中..._"]
        return problem

    def _reset_math_card_state(self) -> None:
        self._math_status_parts = []
        self._math_answer_parts = []
        self._math_status_pending = []
        self._math_answer_pending = []
        self._math_status_typing_current = ""
        self._math_answer_typing_current = ""
        self._math_status_typing_index = 0
        self._math_answer_typing_index = 0

    def _append_math_status(self, text: str) -> None:
        if text.strip():
            self._math_status_pending.append(text)

    def _append_math_answer(self, text: str) -> None:
        if text.strip():
            self._math_answer_pending.append(text)

    def _has_pending_math_typewriter(self) -> bool:
        return bool(
            self._math_status_pending
            or self._math_answer_pending
            or self._math_status_typing_current
            or self._math_answer_typing_current
        )

    def _flush_math_typewriter(self) -> bool:
        progressed = False
        while self._advance_math_typewriter():
            progressed = True
        return progressed

    def _advance_math_typewriter(self) -> bool:
        progressed = False
        progressed |= self._advance_math_status_typewriter()
        progressed |= self._advance_math_answer_typewriter()
        return progressed

    def _advance_math_status_typewriter(self) -> bool:
        if not self._math_status_typing_current and self._math_status_pending:
            self._math_status_typing_current = self._math_status_pending.pop(0)
            self._math_status_typing_index = 0
            self._math_status_parts.append("")
        if not self._math_status_typing_current:
            return False
        step = max(1, self.config.typewriter_status_chars_per_tick)
        end = min(len(self._math_status_typing_current), self._math_status_typing_index + step)
        self._math_status_parts[-1] = self._math_status_typing_current[:end]
        self._math_status_typing_index = end
        if end >= len(self._math_status_typing_current):
            self._math_status_typing_current = ""
            self._math_status_typing_index = 0
            if len(self._math_status_parts) > 500:
                self._math_status_parts = self._math_status_parts[-500:]
        return True

    def _advance_math_answer_typewriter(self) -> bool:
        if not self._math_answer_typing_current and self._math_answer_pending:
            self._math_answer_typing_current = self._math_answer_pending.pop(0)
            self._math_answer_typing_index = 0
            if self._math_answer_parts == ["_等待讲解中..._"]:
                self._math_answer_parts = []
            self._math_answer_parts.append("")
        if not self._math_answer_typing_current:
            return False
        step = max(1, self.config.typewriter_answer_chars_per_tick)
        end = min(len(self._math_answer_typing_current), self._math_answer_typing_index + step)
        self._math_answer_parts[-1] = self._math_answer_typing_current[:end]
        self._math_answer_typing_index = end
        if end >= len(self._math_answer_typing_current):
            self._math_answer_typing_current = ""
            self._math_answer_typing_index = 0
            if len(self._math_answer_parts) > 300:
                self._math_answer_parts = self._math_answer_parts[-300:]
        return True

    def _render_math_sections(self) -> tuple[str, str]:
        status_source = self._math_status_parts[-max(1, self.config.card_status_max_lines):]
        status_lines = []
        for idx, item in enumerate(status_source, start=1):
            safe_item = self._escape_card_literal(item)
            status_lines.append(f"{idx}. {safe_item}")
        dropped_status = max(0, len(self._math_status_parts) - len(status_source))
        if dropped_status > 0:
            status_lines.insert(0, f"_已折叠更早状态 {dropped_status} 条_")
        if not status_lines:
            status_lines.append("- 暂无状态")

        answer = "\n\n".join([p for p in self._math_answer_parts if p.strip()]).strip()
        answer = self._normalize_math_markdown_for_card(answer)
        if not answer:
            answer = "_等待讲解中..._"
        doc_link = self._render_math_doc_link_markdown()
        if doc_link:
            answer = answer + "\n\n<hr>\n\n" + doc_link if answer else doc_link
        return "\n".join(status_lines), answer

    def _render_math_doc_link_markdown(self) -> str:
        document_id = ""
        if self._math_problem and self._math_problem.doc_document_id:
            document_id = self._math_problem.doc_document_id
        elif self.state.math_summary_doc_id:
            document_id = self.state.math_summary_doc_id
        if not document_id:
            return ""
        link = self._math_summary_doc_link(document_id)
        return f"[查看本题讲解云文档]({link})"

    def _send_math_card(self, chat_id: str) -> str:
        template_id = self._resolve_template_id("math")
        if template_id:
            status_md, answer_md = self._render_math_sections()
            msg_id = self._send_template_card(
                chat_id,
                template_id=template_id,
                raw_text="",
                status_text=status_md,
                answer_text=answer_md,
            )
            if msg_id:
                return msg_id
        card = self._build_math_card_json()
        msg_id = self._send_interactive_card(chat_id, card)
        if msg_id:
            return msg_id
        status_md, answer_md = self._render_math_sections()
        return self._send_template_card(
            chat_id,
            template_id=self.config.card_template_id,
            raw_text="",
            status_text=status_md,
            answer_text=answer_md,
        )

    def _update_math_card(self, force: bool = False) -> None:
        problem = self._math_problem
        if not problem or not problem.card_message_id:
            return
        if not self._is_selected_chat(problem.chat_id):
            return
        progressed = self._advance_math_typewriter()
        now = time.monotonic()
        if not force and (now - problem.last_card_push_ts) < self.config.card_update_interval_seconds:
            return
        if not force and not progressed:
            return
        template_id = self._resolve_template_id("math")
        if template_id:
            status_md, answer_md = self._render_math_sections()
            if self._patch_card_message(
                problem.card_message_id,
                template_id=template_id,
                raw_text="",
                status_text=status_md,
                answer_text=answer_md,
            ):
                problem.last_card_push_ts = now
                return
        card = self._build_math_card_json()
        if self._patch_interactive_card(problem.card_message_id, card):
            problem.last_card_push_ts = now
            return
        self._log(f"[bridge] math card update failed problem_id={problem.problem_id} message_id={problem.card_message_id}")

    def _handle_math_tutor_input(self, chat_id: str, sender_open_id: str, text: str, image_key: str, message_id: str) -> None:
        if not text and not image_key:
            return
        if self._is_math_running():
            if self._math_problem:
                self._append_math_status("上一条讲解仍在生成，请稍后或发送 /ctrlc。")
                self._update_math_card(force=True)
            else:
                self._send_text(chat_id, "上一条讲解仍在生成，请稍后或发送 /ctrlc。")
            return

        problem = self._math_problem
        if not problem:
            problem = self._create_math_problem(chat_id, sender_open_id)

        image_path = ""
        if image_key:
            image_path = self._download_message_image(message_id=message_id, image_key=image_key)
            if not image_path:
                self._send_text(chat_id, "下载题目图片失败，请稍后重试。")
                return
            problem.image_paths.append(image_path)
            problem.user_inputs.append(f"[题图] {Path(image_path).name}")
            problem.doc_synced = False
            self._append_math_status(f"收到第 {problem.problem_index} 题图片。")
        if text:
            problem.user_inputs.append(text)
            problem.doc_synced = False
            self._append_math_status(f"收到文字：{text}")

        if not problem.card_message_id:
            self._append_math_status(f"第 {problem.problem_index} 题开始讲解。")
            problem.card_message_id = self._send_math_card(chat_id)
        if problem.thread_id:
            self._append_math_status("继续讲解当前这道题。")
        problem.status = "running"
        problem.updated_at = datetime.now().isoformat(timespec="seconds")
        self._append_math_status("正在生成讲解。")
        self._update_math_card(force=True)

        thread = threading.Thread(
            target=self._run_math_tutor_turn,
            args=(problem, text, image_path),
            daemon=True,
        )
        self._math_runner_thread = thread
        thread.start()

    def _run_math_tutor_turn(self, problem: MathTutorProblem, text: str, image_path: str) -> None:
        prompt = self._build_math_tutor_prompt(problem, text=text, has_image=bool(image_path))
        command = self._math_codex_command(problem.thread_id, prompt, [image_path] if image_path else [])
        self._log(f"[bridge] math exec -> {' '.join(shlex.quote(part) for part in command)}")
        process = None
        interrupted = False
        turn_answer_parts: list[str] = []
        try:
            process = pexpect.spawn(
                "/bin/zsh",
                ["-lc", " ".join(shlex.quote(part) for part in command)],
                encoding="utf-8",
                echo=False,
                codec_errors="ignore",
                cwd=os.getcwd(),
            )
            with self._math_lock:
                self._math_process = process

            header_buffer = ""
            answer_started = False
            while True:
                try:
                    chunk = process.read_nonblocking(size=512, timeout=0.2)
                except pexpect.TIMEOUT:
                    if not self._math_process_alive(process):
                        break
                    continue
                except pexpect.EOF:
                    break

                cleaned = ANSI_RE.sub("", chunk).replace("\r\n", "\n").replace("\r", "\n")
                if not cleaned:
                    continue
                self._log(f"[bridge] math raw output: {cleaned.rstrip()}")

                if not answer_started:
                    header_buffer += cleaned
                    if not problem.thread_id:
                        match = re.search(r"session id:\s*([0-9a-f-]+)", header_buffer, re.IGNORECASE)
                        if match:
                            problem.thread_id = match.group(1).strip()
                            self._append_math_status("已建立数学辅导会话。")
                            self._update_math_card()
                    marker_index = header_buffer.find("\ncodex\n")
                    if marker_index < 0 and header_buffer.startswith("codex\n"):
                        marker_index = 0
                    if marker_index >= 0:
                        answer_started = True
                        answer_text = header_buffer[marker_index + len("\ncodex\n") :] if marker_index > 0 else header_buffer[len("codex\n") :]
                        header_buffer = ""
                        if answer_text:
                            turn_answer_parts.append(answer_text)
                            self._append_math_answer(answer_text)
                            self._update_math_card()
                    else:
                        header_buffer = header_buffer[-4000:]
                    continue

                turn_answer_parts.append(cleaned)
                self._append_math_answer(cleaned)
                self._update_math_card()

            process.close()
            rc = process.exitstatus if process.exitstatus is not None else process.signalstatus
            interrupted = bool(process.signalstatus)
            if rc not in (0, None) and problem.status != "completed":
                self._append_math_status(f"讲解进程退出，返回码 {rc}。")
        except Exception as exc:
            self._append_math_status(f"数学辅导执行失败：{exc}")
        finally:
            with self._math_lock:
                if self._math_process is process:
                    self._math_process = None
            if problem.status != "completed" and not interrupted:
                problem.status = "completed"
                problem.finished_at = datetime.now().isoformat(timespec="seconds")
                self._append_math_status("本轮讲解完成。")
            completed_answer = "".join(turn_answer_parts).strip()
            if completed_answer and not interrupted:
                problem.answer_parts.append(completed_answer)
            problem.updated_at = datetime.now().isoformat(timespec="seconds")
            if not interrupted and problem.answer_parts:
                self._sync_math_problem_to_doc(problem)
            self._flush_math_typewriter()
            self._update_math_card(force=True)

    def _is_math_running(self) -> bool:
        with self._math_lock:
            process = self._math_process
        return self._math_process_alive(process)

    def _interrupt_math_tutor(self) -> bool:
        with self._math_lock:
            process = self._math_process
        if not self._math_process_alive(process):
            return False
        try:
            self._terminate_math_process(process)
        except Exception:
            return False
        if self._math_problem:
            self._math_problem.status = "interrupted"
            self._math_problem.finished_at = datetime.now().isoformat(timespec="seconds")
            self._append_math_status("本轮讲解被 Ctrl+C 中断。")
        return True

    @staticmethod
    def _math_process_alive(process: object) -> bool:
        if process is None:
            return False
        if hasattr(process, "isalive"):
            try:
                return bool(process.isalive())
            except Exception:
                return False
        if hasattr(process, "poll"):
            try:
                return process.poll() is None
            except Exception:
                return False
        return False

    @staticmethod
    def _terminate_math_process(process: object) -> None:
        if hasattr(process, "terminate"):
            try:
                process.terminate(force=True)
                return
            except TypeError:
                process.terminate()
                return
        if hasattr(process, "kill"):
            process.kill()

    def _finalize_math_problem(self, sync_doc: bool) -> None:
        problem = self._math_problem
        if not problem:
            return
        if sync_doc:
            self._sync_math_problem_to_doc(problem)
        self._math_problem = None
        self._math_runner_thread = None
        self._reset_math_card_state()

    def _math_system_prompt(self) -> str:
        custom_prompt = self.state.math_tutor_system_prompt or self.config.math_tutor_system_prompt
        base_prompt = custom_prompt.strip() or (
            "你是一名严谨、耐心的数学辅导老师。你的目标是帮助学生学会做题，而不是只给结论。"
        )
        fixed_rules = (
            "固定要求：\n"
            "1. 全程使用中文。\n"
            "2. 使用 Markdown 输出。\n"
            "3. 所有公式统一使用单个 $...$，不要使用 $$...$$、\\[...\\]、\\(...\\)。\n"
            "4. 先识别题意，再分步讲解，最后给出结论。\n"
            "5. 如果学生发来图片，先简要转写题目再讲解。\n"
            "6. 优先给出可学习、可复用的解题思路。\n"
            "7. 说明性中文不要放进公式内部，例如不要输出 $\\text{计算极限 ...}$。\n"
            "8. 不要执行与解题无关的 shell 命令。"
        )
        return f"{base_prompt}\n\n{fixed_rules}"

    def _build_math_tutor_prompt(self, problem: MathTutorProblem, text: str, has_image: bool) -> str:
        if not problem.thread_id:
            parts = [
                "[数学辅导系统提示词]",
                self._math_system_prompt(),
                "",
                "[题目上下文]",
            ]
        else:
            parts = [
                "继续围绕同一道数学题辅导学生。",
                "仍然使用中文 Markdown 输出，所有公式继续统一使用单个 $...$。",
                "",
                "[学生新消息]",
            ]
        if text:
            parts.append(text)
        if has_image:
            parts.append("学生附带了一张新的题目图片，请结合图片内容继续讲解。")
        return "\n".join(parts).strip()

    def _math_codex_command(self, thread_id: str, prompt: str, image_paths: list[str]) -> list[str]:
        command = ["codex", "exec"]
        if thread_id:
            command.append("resume")
            command.extend(["--skip-git-repo-check"])
        else:
            command.extend(["--color", "never", "--skip-git-repo-check", "-s", "read-only"])
        for image_path in image_paths:
            if image_path:
                command.extend(["--image", image_path])
        if thread_id:
            command.extend(["--", thread_id, prompt])
        else:
            command.extend(["--", prompt])
        return command

    @classmethod
    def _normalize_math_markdown_for_card(cls, text: str) -> str:
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"\$\$(.*?)\$\$", lambda m: cls._format_math_block_for_card(m.group(1)), normalized, flags=re.S)
        normalized = re.sub(r"\\\[(.*?)\\\]", lambda m: cls._format_math_block_for_card(m.group(1)), normalized, flags=re.S)
        normalized = re.sub(r"\\\((.*?)\\\)", lambda m: cls._format_math_inline_for_card(m.group(1)), normalized, flags=re.S)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    @classmethod
    def _format_math_block_for_card(cls, body: str) -> str:
        content = re.sub(r"\s+", " ", body.strip())
        if not content:
            return ""
        match = re.match(r"\\text\{([^{}]+)\}\s*(.*)$", content)
        if match:
            prefix = match.group(1).strip()
            rest = match.group(2).strip()
            if rest:
                return f"{prefix} {cls._format_math_inline_for_card(rest)}"
            return prefix
        return cls._format_math_inline_for_card(content)

    @staticmethod
    def _format_math_inline_for_card(body: str) -> str:
        content = re.sub(r"\s+", " ", body.strip())
        if not content:
            return ""
        return f"$ {content} $"

    def _download_message_image(self, message_id: str, image_key: str) -> str:
        resp = None
        if message_id:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            resp = self.api_client.im.v1.message_resource.get(req)
            if resp.success() and getattr(resp, "file", None):
                return self._save_binary_asset(resp.file, getattr(resp, "file_name", "") or f"{image_key}.png")
            self._log(
                f"[bridge] message resource image download failed message_id={message_id} image_key={image_key} "
                f"code={resp.code} msg={resp.msg}"
            )

        req = GetImageRequest.builder().image_key(image_key).build()
        resp = self.api_client.im.v1.image.get(req)
        if not resp.success() or not getattr(resp, "file", None):
            self._log(f"[bridge] download image failed message_id={message_id} image_key={image_key} code={resp.code} msg={resp.msg}")
            return ""
        return self._save_binary_asset(resp.file, getattr(resp, "file_name", "") or f"{image_key}.png")

    def _save_binary_asset(self, file_obj, file_name: str) -> str:
        suffix = Path(file_name or "image.png").suffix or ".png"
        file_path = self._math_asset_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}{suffix}"
        with file_path.open("wb") as f:
            f.write(file_obj.read())
        return str(file_path)

    def _create_math_summary_document(self, title: str) -> str:
        self._last_math_doc_error = ""
        req_body = CreateDocumentRequestBody.builder().title(title).build()
        if self.config.math_tutor_doc_folder_token:
            req_body.folder_token = self.config.math_tutor_doc_folder_token
        req = CreateDocumentRequest.builder().request_body(req_body).build()
        resp = self.api_client.docx.v1.document.create(req)
        if not resp.success() or not resp.data or not resp.data.document:
            self._last_math_doc_error = "创建数学总结文档失败：飞书接口返回错误，请检查机器人文档权限。"
            self._log(f"[bridge] create math doc failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
            return ""
        document_id = resp.data.document.document_id or ""
        if document_id:
            self._ensure_math_summary_doc_access(document_id)
            self.state.set_math_summary_doc(document_id, title)
        return document_id

    def _ensure_math_summary_document(self, problem: Optional[MathTutorProblem] = None) -> str:
        document_id = self.state.math_summary_doc_id
        if document_id and self._math_summary_document_exists(document_id):
            return document_id
        if document_id:
            self._log(f"[bridge] stale math summary doc cleared document_id={document_id}")
            self.state.clear_math_summary_doc()
            if problem:
                problem.doc_document_id = ""
                problem.doc_start_index = -1
                problem.doc_block_count = 0
        title = self.state.math_summary_doc_title or self.config.math_tutor_doc_title
        document_id = self._create_math_summary_document(title)
        if document_id and problem:
            self._append_math_status(f"已固定数学总结文档：{document_id}")
        return document_id

    def _sync_math_problem_to_doc(self, problem: MathTutorProblem) -> None:
        if problem.doc_synced:
            return
        if not problem.user_inputs and not problem.answer_parts:
            return
        document_id = problem.doc_document_id or self.state.math_summary_doc_id
        if document_id and not self._math_summary_document_exists(document_id):
            self._log(f"[bridge] math summary doc missing before sync document_id={document_id}")
            if document_id == self.state.math_summary_doc_id:
                self.state.clear_math_summary_doc()
            problem.doc_document_id = ""
            problem.doc_start_index = -1
            problem.doc_block_count = 0
            document_id = ""
        if not document_id:
            document_id = self._ensure_math_summary_document(problem)
        if not document_id:
            return
        markdown = self._build_math_summary_markdown(problem)
        blocks = self._convert_markdown_to_blocks(markdown)
        if not blocks:
            self._append_math_status("同步飞书文档失败：Markdown 转块失败。")
            self._flush_math_typewriter()
            self._update_math_card(force=True)
            return
        pending_start_index = problem.doc_start_index
        if problem.doc_start_index >= 0 and problem.doc_block_count > 0:
            ok, block_count, error_code, error_msg = self._replace_math_problem_doc_blocks(
                document_id=document_id,
                start_index=problem.doc_start_index,
                old_count=problem.doc_block_count,
                blocks=blocks,
            )
            action = "更新"
        else:
            start_index = self._document_child_count(document_id)
            pending_start_index = start_index
            ok, block_count, error_code, error_msg = self._append_blocks_to_document(document_id, start_index, blocks)
            action = "同步"
        problem.doc_document_id = document_id
        if pending_start_index >= 0:
            problem.doc_start_index = pending_start_index
        if block_count > 0:
            problem.doc_block_count = block_count
        if not ok:
            if self._is_resource_deleted_error(error_code, error_msg):
                self._log(f"[bridge] math summary doc deleted during sync document_id={document_id}")
                if document_id == self.state.math_summary_doc_id:
                    self.state.clear_math_summary_doc()
                problem.doc_document_id = ""
                problem.doc_start_index = -1
                problem.doc_block_count = 0
                recreated_document_id = self._ensure_math_summary_document(problem)
                if recreated_document_id:
                    retry_start_index = self._document_child_count(recreated_document_id)
                    retry_ok, retry_block_count, retry_error_code, retry_error_msg = self._append_blocks_to_document(
                        recreated_document_id,
                        retry_start_index,
                        blocks,
                    )
                    if retry_ok:
                        problem.doc_document_id = recreated_document_id
                        problem.doc_start_index = retry_start_index
                        problem.doc_block_count = retry_block_count
                        problem.doc_synced = True
                        self._append_math_status(f"原文档已失效，已自动迁移到新文档：{recreated_document_id}")
                        self._flush_math_typewriter()
                        self._update_math_card(force=True)
                        return
            self._append_math_status("同步飞书文档失败：写入文档接口报错。")
            self._flush_math_typewriter()
            self._update_math_card(force=True)
            return
        problem.doc_synced = True
        self._append_math_status(f"本题已{action}到飞书文档：{document_id}")
        self._flush_math_typewriter()
        self._update_math_card(force=True)

    def _append_blocks_to_document(self, document_id: str, index: int, blocks: list) -> tuple[bool, int, int, str]:
        if not blocks:
            return True, 0, 0, ""
        total_created = 0
        next_index = index
        for offset in range(0, len(blocks), DOCX_CREATE_CHILDREN_BATCH_SIZE):
            batch = blocks[offset : offset + DOCX_CREATE_CHILDREN_BATCH_SIZE]
            req = (
                CreateDocumentBlockChildrenRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .document_revision_id(-1)
                .request_body(
                    CreateDocumentBlockChildrenRequestBody.builder()
                    .children(batch)
                    .index(next_index)
                    .build()
                )
                .build()
            )
            resp = self.api_client.docx.v1.document_block_children.create(req)
            if not resp.success():
                self._log(
                    f"[bridge] append math doc failed code={resp.code} msg={resp.msg} "
                    f"log_id={resp.get_log_id()} batch_start={offset} batch_size={len(batch)}"
                )
                return False, total_created, int(resp.code or 0), str(resp.msg or "")
            created = list(resp.data.children or []) if resp.data else []
            created_count = len(created) or len(batch)
            total_created += created_count
            next_index += created_count
            if offset + DOCX_CREATE_CHILDREN_BATCH_SIZE < len(blocks):
                time.sleep(DOCX_WRITE_INTERVAL_SECONDS)
        return True, total_created, 0, ""

    def _replace_math_problem_doc_blocks(
        self,
        document_id: str,
        start_index: int,
        old_count: int,
        blocks: list,
    ) -> tuple[bool, int, int, str]:
        if old_count > 0:
            delete_req = (
                BatchDeleteDocumentBlockChildrenRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .document_revision_id(-1)
                .request_body(
                    BatchDeleteDocumentBlockChildrenRequestBody.builder()
                    .start_index(start_index)
                    .end_index(start_index + old_count)
                    .build()
                )
                .build()
            )
            delete_resp = self.api_client.docx.v1.document_block_children.batch_delete(delete_req)
            if not delete_resp.success():
                self._log(
                    f"[bridge] replace math doc delete failed code={delete_resp.code} "
                    f"msg={delete_resp.msg} log_id={delete_resp.get_log_id()}"
                )
                return False, old_count, int(delete_resp.code or 0), str(delete_resp.msg or "")
        return self._append_blocks_to_document(document_id, start_index, blocks)

    def _convert_markdown_to_blocks(self, markdown: str) -> list:
        req = (
            ConvertDocumentRequest.builder()
            .request_body(
                ConvertDocumentRequestBody.builder()
                .content_type("markdown")
                .content(markdown)
                .build()
            )
            .build()
        )
        resp = self.api_client.docx.v1.document.convert(req)
        if not resp.success() or not resp.data:
            self._log(f"[bridge] convert markdown failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
            return []
        return list(resp.data.blocks or [])

    def _document_child_count(self, document_id: str) -> int:
        page_token = ""
        count = 0
        while True:
            builder = (
                GetDocumentBlockChildrenRequest.builder()
                .document_id(document_id)
                .block_id(document_id)
                .page_size(200)
            )
            if page_token:
                builder.page_token(page_token)
            resp = self.api_client.docx.v1.document_block_children.get(builder.build())
            if not resp.success() or not resp.data:
                self._log(f"[bridge] list doc children failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
                return count
            items = list(resp.data.items or [])
            count += len(items)
            if not getattr(resp.data, "has_more", False):
                return count
            page_token = getattr(resp.data, "page_token", "") or ""
            if not page_token:
                return count

    def _ensure_math_summary_doc_access(self, document_id: str) -> None:
        req = GetPermissionPublicRequest.builder().token(document_id).type("docx").build()
        resp = self.api_client.drive.v1.permission_public.get(req)
        if not resp.success() or not resp.data or not resp.data.permission_public:
            self._log(
                f"[bridge] get math doc public permission failed document_id={document_id} "
                f"code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
            )
            return
        permission = resp.data.permission_public
        if getattr(permission, "link_share_entity", "") == "tenant_readable":
            return
        patch_req = (
            PatchPermissionPublicRequest.builder()
            .token(document_id)
            .type("docx")
            .request_body(PermissionPublicRequest.builder().link_share_entity("tenant_readable").build())
            .build()
        )
        patch_resp = self.api_client.drive.v1.permission_public.patch(patch_req)
        if not patch_resp.success():
            self._log(
                f"[bridge] patch math doc public permission failed document_id={document_id} "
                f"code={patch_resp.code} msg={patch_resp.msg} log_id={patch_resp.get_log_id()}"
            )

    def _math_summary_document_exists(self, document_id: str) -> bool:
        if not document_id:
            return False
        req = GetDocumentRequest.builder().document_id(document_id).build()
        resp = self.api_client.docx.v1.document.get(req)
        if resp.success() and resp.data and resp.data.document:
            return True
        if self._is_resource_deleted_error(int(resp.code or 0), str(resp.msg or "")):
            return False
        self._log(
            f"[bridge] get math doc failed document_id={document_id} code={resp.code} "
            f"msg={resp.msg} log_id={resp.get_log_id()}"
        )
        return True

    @staticmethod
    def _is_resource_deleted_error(code: int, msg: str) -> bool:
        return code == 1770003 or "resource deleted" in (msg or "").lower()

    @staticmethod
    def _math_summary_doc_link(document_id: str) -> str:
        return f"https://my.feishu.cn/docx/{document_id}"

    def _build_math_summary_markdown(self, problem: MathTutorProblem) -> str:
        question_lines = []
        for idx, item in enumerate(problem.user_inputs, start=1):
            question_lines.append(f"{idx}. {item}")
        if problem.image_paths:
            question_lines.append(f"- 学生共发送题图 {len(problem.image_paths)} 张。")
        answer = "\n\n---\n\n".join([part for part in problem.answer_parts if part.strip()]).strip() or "（暂无解答）"
        title = f"第 {problem.problem_index} 题"
        return (
            f"# {title}\n\n"
            f"- 时间：{problem.started_at}\n"
            f"- 问题 ID：`{problem.problem_id}`\n\n"
            "## 题目内容\n\n"
            f"{chr(10).join(question_lines) if question_lines else '（用户未提供文字题面）'}\n\n"
            "## 讲解与解答\n\n"
            f"{answer}\n"
        )

    @staticmethod
    def _extract_document_id(raw: str) -> str:
        text = raw.strip()
        if not text:
            return ""
        match = re.search(r"/docx/([A-Za-z0-9]+)", text)
        if match:
            return match.group(1)
        match = re.search(r"\b([A-Za-z0-9]{10,})\b", text)
        if match:
            return match.group(1)
        return ""

    def _extract_text(self, message_type: Optional[str], content: Optional[str]) -> str:
        if message_type != "text" or not content:
            return ""
        try:
            body = json.loads(content)
            return str(body.get("text", "")).strip()
        except Exception:
            return ""

    def _extract_image_key(self, message_type: Optional[str], content: Optional[str]) -> str:
        if message_type != "image" or not content:
            return ""
        try:
            body = json.loads(content)
        except Exception:
            return ""
        return str(body.get("image_key") or "").strip()

    def _normalize_incoming_text(self, text: str) -> str:
        if not text:
            return ""
        cleaned = text.replace("\u200b", " ").strip()
        cleaned = MENTION_TOKEN_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _command_allowed(self, command: str) -> bool:
        if not self.config.allowed_prefixes:
            return True
        return command.startswith(self.config.allowed_prefixes)

    def _help_text(self) -> str:
        return (
            "可用命令:\n"
            "/bind - 绑定当前会话，并重置当前客户端选择\n"
            "/clients - 查看当前这台机器对应的客户端信息；多台机器同时在线时可用来枚举客户端\n"
            "/use <client_id> - 选择某个客户端处理当前会话\n"
            "/leaveclient - 释放当前会话上的客户端选择\n"
            "/status - 查看当前客户端状态\n"
            "/cmd <command> - 在本地终端执行命令\n"
            "/codex - 先询问 model 和 permission，再进入 Codex 对话模式\n"
            "/exitcodex - 退出或取消进入 Codex 对话模式\n"
            "教我做题 - 进入数学辅导模式（支持文字题和图片题）\n"
            "下一题 - 在数学辅导模式下开启新题，下一题使用新卡片\n"
            "结束做题 - 退出数学辅导模式\n"
            "设置数学辅导提示词 <内容> - 更新数学辅导系统提示词\n"
            "查看数学辅导提示词 / 清空数学辅导提示词 - 查看或恢复默认提示词\n"
            "创建数学总结文档 [标题] / 绑定数学总结文档 <doc_id> - 配置固定飞书文档；未绑定时会在首次同步时自动创建\n"
            "查看数学总结文档 / 关闭数学总结文档 - 查看或关闭文档同步\n"
            "/history - 查看最近任务；/history <task_id> 查看详情；/history clear 清空历史\n"
            "/ctrlc - 给终端发送 Ctrl+C\n"
            "说明: 普通模式复用同一张系统卡片；Codex 模式按请求分卡；数学辅导模式按“题目”分卡，同题追问继续写入同一卡片；"
            "多客户端并行时，先 /clients 再 /use <client_id>，只有被选中的客户端会继续处理普通命令"
        )

    def _send_bound_chat(self, text: str) -> None:
        if not self.state.bound_chat_id:
            return
        if not self._is_selected_chat(self.state.bound_chat_id):
            return
        self._send_text(self.state.bound_chat_id, text)

    def _send_text(self, chat_id: str, text: str, force_new: bool = False) -> None:
        try:
            if self._upsert_general_card(chat_id, text, force_new=force_new):
                return
            self._send_text_standalone(chat_id, text)
        except Exception as exc:
            self._log(f"[bridge] send exception chat_id={chat_id} err={exc}")

    def _upsert_general_card(self, chat_id: str, text: str, force_new: bool = False) -> bool:
        if not text.strip():
            return False
        if force_new:
            self._reset_general_card_state()
        self._general_feed_parts.append(text)
        if len(self._general_feed_parts) > 120:
            self._general_feed_parts = self._general_feed_parts[-120:]
        template_id = self._resolve_template_id("general")
        if template_id:
            status_md, answer_md = self._render_general_sections()
            if self._general_card_message_id and self._patch_card_message(
                self._general_card_message_id,
                template_id=template_id,
                raw_text=text,
                status_text=status_md,
                answer_text=answer_md,
            ):
                self._log(f"[bridge] general card updated message_id={self._general_card_message_id}")
                return True
            msg_id = self._send_template_card(
                chat_id,
                template_id=template_id,
                raw_text=text,
                status_text=status_md,
                answer_text=answer_md,
            )
            if msg_id:
                self._general_card_message_id = msg_id
                return True

        card = self._build_general_card_json()
        if self._general_card_message_id and self._patch_interactive_card(self._general_card_message_id, card):
            self._log(f"[bridge] general card updated message_id={self._general_card_message_id}")
            return True
        msg_id = self._send_interactive_card(chat_id, card)
        if not msg_id:
            return False
        self._general_card_message_id = msg_id
        return True

    def _render_general_sections(self) -> tuple[str, str]:
        items = self._general_feed_parts[-12:]
        dropped = max(0, len(self._general_feed_parts) - len(items))
        if self.math_tutor_mode:
            mode_text = "数学辅导中"
        elif self.codex_mode:
            mode_text = "Codex 对话中"
        else:
            mode_text = "普通终端模式"
        status_lines = [f"系统消息面板", f"当前模式: {mode_text}"]
        if dropped > 0:
            status_lines.append(f"_已折叠更早消息 {dropped} 条_")
        answer = "\n\n<hr>\n\n".join(item.strip() for item in items if item.strip()).strip()
        if not answer:
            answer = "_暂无消息_"
        return "\n".join(status_lines), answer

    def _send_text_standalone(self, chat_id: str, text: str) -> None:
        if self._send_markdown(chat_id, text):
            return
        if self._send_post(chat_id, text):
            return
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api_client.im.v1.message.create(req)
        if not resp.success():
            self._log(
                f"[bridge] send failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
            )
            return
        self._log(f"[bridge] send ok(text) chat_id={chat_id} size={len(text)}")

    def _send_markdown(self, chat_id: str, text: str) -> bool:
        template_id = self._resolve_template_id("general")
        if template_id:
            msg_id = self._send_template_card(
                chat_id,
                template_id=template_id,
                raw_text=text,
                status_text="系统消息",
                answer_text=text,
            )
            if msg_id:
                return True
        card = self._build_rich_card(
            title="OpenCodex",
            subtitle="系统消息",
            template="grey",
            summary=self._truncate_inline_text(text, CARD_SUMMARY_TEXT_LIMIT) or "系统消息",
            tags=[self._header_tag("System", "neutral")],
            elements=[self._markdown_element(chunk) for chunk in self._split_markdown_for_card(text) or ["_暂无消息_"]],
        )
        msg_id = self._send_interactive_card(chat_id, card)
        if not msg_id:
            msg_id = self._send_template_card(
                chat_id,
                template_id=self.config.card_template_id,
                raw_text=text,
                status_text="系统消息",
                answer_text=text,
            )
        return bool(msg_id)

    def _patch_card_message(
        self,
        message_id: str,
        template_id: str,
        raw_text: str,
        status_text: str = "",
        answer_text: str = "",
    ) -> bool:
        content = self._template_card_content(
            template_id=template_id,
            raw_text=raw_text,
            status_text=status_text,
            answer_text=answer_text,
        )
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api_client.im.v1.message.patch(req)
        if resp.success():
            return True
        self._log(
            f"[bridge] patch failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()} message_id={message_id}"
        )
        return False

    def _patch_interactive_card(self, message_id: str, card_content: dict) -> bool:
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card_content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api_client.im.v1.message.patch(req)
        if resp.success():
            return True
        self._log(
            f"[bridge] patch interactive card failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()} message_id={message_id}"
        )
        return False

    def _template_card_content(
        self,
        template_id: str,
        raw_text: str,
        status_text: str = "",
        answer_text: str = "",
    ) -> dict:
        safe_raw = raw_text if raw_text.strip() else " "
        safe_status = status_text if status_text.strip() else " "
        safe_answer = answer_text if answer_text.strip() else " "
        variables = {}
        if self.config.card_status_var_name and self.config.card_answer_var_name:
            variables[self.config.card_status_var_name] = safe_status
            variables[self.config.card_answer_var_name] = safe_answer
        else:
            variables[self.config.card_template_var_name] = safe_raw
        return {
            "type": "template",
            "data": {
                "template_id": template_id,
                "template_variable": variables,
            },
        }

    def _send_template_card(
        self,
        chat_id: str,
        template_id: str,
        raw_text: str,
        status_text: str = "",
        answer_text: str = "",
    ) -> str:
        safe_text = raw_text if raw_text.strip() else " "
        if not template_id:
            self._log("[bridge] schema2 template card skipped: CARD_TEMPLATE_ID is empty")
            return ""

        card = self._template_card_content(
            template_id=template_id,
            raw_text=safe_text,
            status_text=status_text,
            answer_text=answer_text,
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api_client.im.v1.message.create(req)
        if resp.success():
            msg_type = getattr(resp.data, "msg_type", None) if resp.data else None
            msg_id = getattr(resp.data, "message_id", None) if resp.data else None
            self._log(
                f"[bridge] send ok(schema2-template-card) chat_id={chat_id} size={len(safe_text)}"
                f" msg_type={msg_type} message_id={msg_id}"
            )
            return msg_id or ""
        self._log(
            f"[bridge] send schema2-template-card failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
        )
        return ""

    def _send_interactive_card(self, chat_id: str, card_content: dict) -> str:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card_content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api_client.im.v1.message.create(req)
        if resp.success():
            msg_type = getattr(resp.data, "msg_type", None) if resp.data else None
            msg_id = getattr(resp.data, "message_id", None) if resp.data else None
            self._log(
                f"[bridge] send ok(interactive-card-v2) chat_id={chat_id}"
                f" msg_type={msg_type} message_id={msg_id}"
            )
            return msg_id or ""
        self._log(
            f"[bridge] send interactive card failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
        )
        return ""

    def _send_post(self, chat_id: str, text: str) -> bool:
        lines = [ln for ln in text.split("\n")]
        content_blocks = []
        for ln in lines:
            ln = ln.rstrip()
            if not ln:
                content_blocks.append([{"tag": "text", "text": " "}])
            else:
                content_blocks.append([{"tag": "text", "text": ln}])

        post_payload = {
            "zh_cn": {
                "title": "OpenCodex",
                "content": content_blocks,
            }
        }
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("post")
                .content(json.dumps(post_payload, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.api_client.im.v1.message.create(req)
        if not resp.success():
            self._log(
                f"[bridge] send post failed code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}"
            )
            return False
        self._log(f"[bridge] send ok(post) chat_id={chat_id} size={len(text)}")
        return True

    def _log(self, message: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} {message}"
        with self._log_lock:
            print(line)
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


def main() -> None:
    config = Config.load()
    bridge = FeishuCodexBridge(config)

    def _graceful_stop(_signum, _frame):
        bridge.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _graceful_stop)
    signal.signal(signal.SIGTERM, _graceful_stop)

    bridge.start()


if __name__ == "__main__":
    main()
