#!/usr/bin/env python3
"""Portable stdio file client for the central academic rewriting service."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

MAX_FILE = 10 * 1024 * 1024
MAX_RESULT = 40 * 1024 * 1024
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_JSON = 1024 * 1024
MAX_ENROLLMENT_JSON = 4096
DEFAULT_URL = "https://64.83.38.67:9443"
DEFAULT_CONFIG = Path.home() / ".academic-rewrite" / "config.json"
DEFAULT_STATE = Path.home() / ".academic-rewrite" / "receipts"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "scripts" else Path(__file__).resolve().parent
DEFAULT_CA = PLUGIN_ROOT / "assets" / "server-ca.pem"
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
IS_WINDOWS = os.name == "nt"
PUBLIC_CODES = {"invalid_input", "invalid_file", "unsupported_file", "file_too_large", "invalid_platform", "invalid_combination",
                "unknown_platform", "pro_unavailable", "platform_unavailable", "unauthorized", "access_denied",
                "key_revoked", "forbidden", "not_found", "not_ready", "job_not_found", "job_not_ready", "busy_not_started",
                "request_id_conflict", "request_already_attempted", "account_disabled", "login_backoff",
                "upstream_maintenance", "upstream_rejected", "upstream_http", "upstream_response",
                "network_unknown", "result_unknown", "internal_error", "service_unavailable", "rate_limited",
                "quota_exceeded", "literature_unavailable", "download_failed", "invalid_citation", "literature_quota",
                "literature_not_found", "literature_timeout", "invalid_result", "already_running", "storage_full",
                "non_pdf_result", "download_link_unavailable", "download_adapter_unavailable", "session_expired",
                "document_integrity", "docx_required", "no_safe_text", "enrollment_disabled", "enrollment_limited",
                "upstream_access_denied", "partial_failure", "feature_disabled"}
MESSAGES = {
    "need_platform": "请选择本次文件使用的检测平台。",
    "network_unknown": "连接中断，任务结果未知；请使用原 job_id 查询，未自动重发。",
    "job_not_ready": "文件尚未完成，请稍后查询同一任务。",
    "request_already_attempted": "本次任务已提交或结果未知；请查询原任务，未再次提交。",
    "configuration": "无法读取共享服务配置，请在本机配置服务地址、访问密钥和可信证书。",
    "enrollment_disabled": "服务暂未开放新设备接入，请联系服务提供者。",
    "enrollment_limited": "新设备接入暂时达到限额，请稍后重新运行安装。",
    "enrollment_network": "新设备接入未完成，请检查网络后重新运行；本机安装凭据已保留，未提交改写任务。",
    "enrollment_busy": "本机接入配置正在准备，请稍后重新运行。",
    "access_denied": "此设备访问已停用或接入凭据不匹配，请联系服务提供者。",
    "invalid_input": "工具参数无效。",
    "invalid_file": "请选择有效的 DOC 或 DOCX 文件。",
    "docx_required": "保护改写需 DOCX，请先将 DOC 另存为 DOCX；未提交付费任务。",
    "no_safe_text": "这份稿件没有可安全改写的普通段落，已停止收费提交。",
    "document_integrity": "返回稿无法可靠对应原稿，已保留服务原件，停止交付；未自动重发。",
    "file_too_large": "文件不能超过 10MB。",
    "local_file": "无法读取或保存本机文件。",
    "response_invalid": "共享服务响应格式异常，未自动重发。",
    "redirect_blocked": "服务要求跳转，已阻止密钥转发。",
    "storage_full": "共享服务存储空间不足，暂时无法创建任务，请联系服务提供者。",
    "non_pdf_result": "返回内容不是有效PDF，本篇未交付。",
    "download_link_unavailable": "本篇暂无可用下载链接。",
    "download_adapter_unavailable": "文献下载通道暂时不可用，请联系服务提供者。",
    "session_expired": "文献会员登录已失效，请联系服务提供者续期。",
    "literature_quota": "今日共享文献下载尝试额度不足。",
    "literature_timeout": "文献请求超时，本次未自动重试。",
    "upstream_access_denied": "文献来源拒绝访问，本篇未交付。",
    "literature_unavailable": "文献通道暂时不可用。",
    "partial_failure": "部分文献未取得，请查看各篇失败原因。",
    "upstream_http": "文献来源暂时无法完成请求，本次未自动重试。",
    "rate_limited": "文献来源暂时限流，任务已停止；请稍后决定是否重新创建任务。",
    "feature_disabled": "文献下载已关闭；文献检索、书目核验和Word改写仍可使用。",
}


class ShareError(Exception):
    def __init__(self, code, job_id=None):
        self.code = code
        self.job_id = job_id
        super().__init__(MESSAGES.get(code, "共享服务未完成操作，请检查任务状态。"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ShareError("invalid_input")
    return value


def checked_url(value):
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError()
        parts = urllib.parse.urlsplit(value)
        if (not parts.hostname or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.port == 0 or parts.path not in ("", "/")
                or "\\" in value):
            raise ValueError()
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
            raise ValueError()
        return value.rstrip("/")
    except ValueError:
        raise ShareError("configuration") from None


def windows_acl(path, protect=False, directory=False):
    """Use the built-in Windows ACL manager; fail closed if unavailable."""
    script = r'''
$ErrorActionPreference = 'Stop'
$p = $env:ACADEMIC_REWRITE_ACL_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$allowed = @($sid.Value, 'S-1-5-18', 'S-1-5-32-544')
$acl = Get-Acl -LiteralPath $p
if ($env:ACADEMIC_REWRITE_ACL_PROTECT -eq '1') {
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
  $inherit = [System.Security.AccessControl.InheritanceFlags]::None
  if ($env:ACADEMIC_REWRITE_ACL_DIRECTORY -eq '1') {
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
  }
  foreach ($identity in $allowed) {
    $principal = New-Object System.Security.Principal.SecurityIdentifier($identity)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($principal, 'FullControl', $inherit, 'None', 'Allow')
    [void]$acl.AddAccessRule($rule)
  }
  $acl.SetOwner($sid)
  Set-Acl -LiteralPath $p -AclObject $acl
  $acl = Get-Acl -LiteralPath $p
}
if ($acl.Owner -eq $null) { exit 1 }
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
if ($owner -ne $sid.Value) { exit 1 }
$selfAllowed = $false
foreach ($rule in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
  if ($rule.AccessControlType -eq 'Allow') {
    if ($allowed -notcontains $rule.IdentityReference.Value) { exit 1 }
    if ($rule.IdentityReference.Value -eq $sid.Value) { $selfAllowed = $true }
  }
}
if (-not $selfAllowed) { exit 1 }
'''
    environment = dict(os.environ)
    environment.update(ACADEMIC_REWRITE_ACL_PATH=str(path.resolve()),
                       ACADEMIC_REWRITE_ACL_PROTECT="1" if protect else "0",
                       ACADEMIC_REWRITE_ACL_DIRECTORY="1" if directory else "0")
    # Use the trusted OS executable, rather than a command found through PATH.
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode != 0:
            raise ShareError("configuration")
    except (OSError, subprocess.TimeoutExpired):
        raise ShareError("configuration") from None


def ensure_private_file(path):
    if not path.is_file() or path.stat().st_size > 16384:
        raise ShareError("configuration")
    if IS_WINDOWS:
        windows_acl(path)
    elif path.stat().st_mode & 0o077:
        raise ShareError("configuration")


def protect_private_path(path, directory=False):
    if IS_WINDOWS:
        windows_acl(path, protect=True, directory=directory)
    else:
        path.chmod(0o700 if directory else 0o600)


def private_write(path, value):
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_path(path.parent, directory=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        try:
            descriptor_chmod = getattr(os, "fchmod", None)
            if IS_WINDOWS:
                try:
                    protect_private_path(Path(temporary))
                except ShareError:
                    os.close(descriptor)
                    raise
            elif callable(descriptor_chmod):
                descriptor_chmod(descriptor, 0o600)
            else:
                os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        raise ShareError("local_file") from None


def json_object(raw):
    try:
        if len(raw) > MAX_JSON:
            raise ValueError()
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        raise ShareError("response_invalid") from None


def _config_path(path=None):
    return Path(path or os.environ.get("BIXIA_MCP_CONFIG", os.environ.get("ACADEMIC_REWRITE_CONFIG", str(DEFAULT_CONFIG)))).expanduser()


def secure_opener(ca_file=None):
    try:
        context = ssl.create_default_context(cafile=str(ca_file)) if ca_file else ssl.create_default_context()
        return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    except (OSError, ValueError, ssl.SSLError):
        raise ShareError("configuration") from None


def ensure_auto_config(path=None, *, opener=None, progress=None):
    """Create a private per-install access config once; never replace an existing config."""
    destination = _config_path(path)
    if destination.exists() or "BIXIA_MCP_KEY" in os.environ or "ACADEMIC_REWRITE_KEY" in os.environ:
        return destination
    progress = progress if progress is not None else lambda message: print(message, file=sys.stderr)
    proof_path = destination.with_name(destination.name + ".enrollment.json")
    lock_path = destination.with_name(destination.name + ".enrollment.lock")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_path(destination.parent, directory=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise ShareError("local_file") from None
    try:
        protect_private_path(lock_path)
        try:
            if IS_WINDOWS:
                import msvcrt
                os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ShareError("enrollment_busy") from None
        if destination.exists():
            return destination
        if proof_path.exists():
            ensure_private_file(proof_path)
            proof = json_object(proof_path.read_bytes())
            if (set(proof) != {"install_id", "enrollment_secret"}
                    or not isinstance(proof["install_id"], str)
                    or not isinstance(proof["enrollment_secret"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", proof["enrollment_secret"])):
                raise ShareError("configuration")
            try:
                identity = uuid.UUID(proof["install_id"])
            except ValueError:
                raise ShareError("configuration") from None
        else:
            identity = uuid.uuid4()
            proof = {"install_id": str(identity), "enrollment_secret": secrets.token_hex(32)}
            private_write(proof_path, proof)
        url = checked_url(os.environ.get("BIXIA_MCP_URL", os.environ.get("ACADEMIC_REWRITE_URL", DEFAULT_URL)))
        ca = os.environ.get("BIXIA_MCP_CA_FILE", os.environ.get("ACADEMIC_REWRITE_CA_FILE"))
        if ca is None and DEFAULT_CA.is_file():
            ca = DEFAULT_CA
        ca = str(Path(ca).expanduser().resolve()) if ca is not None else None
        opener = opener if opener is not None else secure_opener(ca)
        request = urllib.request.Request(url + "/share/enroll", method="POST",
                                         data=json.dumps(proof).encode("utf-8"),
                                         headers={"Accept": "application/json", "Content-Type": "application/json"})
        progress("BIxia 正在为此设备准备独立访问配置……")
        try:
            with opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise ShareError("response_invalid")
                raw = response.read(MAX_ENROLLMENT_JSON + 1)
                if len(raw) > MAX_ENROLLMENT_JSON:
                    raise ShareError("response_invalid")
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                raise ShareError("redirect_blocked") from None
            try:
                raw = error.read(MAX_ENROLLMENT_JSON + 1)
                envelope = json_object(raw) if len(raw) <= MAX_ENROLLMENT_JSON else {}
                detail = envelope.get("error")
                code = detail.get("code") if isinstance(detail, dict) else None
            except (ShareError, OSError, TimeoutError, http.client.HTTPException):
                code = None
            finally:
                error.close()
            allowed = {"enrollment_disabled", "enrollment_limited", "access_denied", "invalid_input", "service_unavailable"}
            raise ShareError(code if code in allowed else "service_unavailable") from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
            raise ShareError("enrollment_network") from None
        envelope = json_object(raw)
        data = envelope.get("data")
        if (set(envelope) != {"ok", "data"} or envelope.get("ok") is not True or not isinstance(data, dict)
                or set(data) != {"api_key", "owner_id"} or data.get("owner_id") != "install_" + identity.hex
                or not isinstance(data.get("api_key"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["api_key"])):
            raise ShareError("response_invalid")
        value = {"server_url": url, "api_key": data["api_key"]}
        if ca is not None:
            value["ca_file"] = ca
        private_write(destination, value)
        progress("BIxia 此设备的访问配置已保存，未提交改写任务。")
        return destination
    except (OSError, TypeError, ValueError):
        raise ShareError("configuration") from None
    finally:
        os.close(descriptor)


def is_docx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            return (len(entries) <= 5000 and sum(entry.file_size for entry in entries) <= 100 * 1024 * 1024
                    and {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist()))
    except (zipfile.BadZipFile, ValueError, OSError):
        return False


class ShareClient:
    def __init__(self, url, key, ca_file=None, state_dir=None, opener=None):
        self.url = checked_url(url)
        if (not isinstance(key, str) or not 32 <= len(key) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in key)):
            raise ShareError("configuration")
        self.key = key
        self.closed = False
        self.state_dir = Path(state_dir) if state_dir is not None else DEFAULT_STATE
        self.namespace = hashlib.sha256((self.url + "\0" + key).encode()).hexdigest()[:32]
        if opener is None:
            self.opener = secure_opener(ca_file)
        else:
            self.opener = opener

    @classmethod
    def from_config(cls, path=None, state_dir=None, *, allow_missing=False):
        config_path = _config_path(path)
        values = {}
        try:
            if config_path.exists():
                ensure_private_file(config_path)
                values = json_object(config_path.read_bytes())
            elif path is not None and not allow_missing:
                raise ShareError("configuration")
            url = os.environ.get("BIXIA_MCP_URL", os.environ.get("ACADEMIC_REWRITE_URL", values.get("server_url", DEFAULT_URL)))
            key = os.environ.get("BIXIA_MCP_KEY", os.environ.get("ACADEMIC_REWRITE_KEY", values.get("api_key", "")))
            ca = os.environ.get("BIXIA_MCP_CA_FILE", os.environ.get("ACADEMIC_REWRITE_CA_FILE", values.get("ca_file")))
            if ca is None and DEFAULT_CA.is_file():
                ca = DEFAULT_CA
            if ca is not None:
                ca_path = Path(ca).expanduser()
                if not ca_path.is_absolute():
                    ca_path = (Path.cwd() if "BIXIA_MCP_CA_FILE" in os.environ or "ACADEMIC_REWRITE_CA_FILE" in os.environ else config_path.parent) / ca_path
                ca = str(ca_path.resolve())
            return cls(url, key, ca, state_dir)
        except (OSError, TypeError, ValueError):
            raise ShareError("configuration") from None

    def _request(self, method, path, body=None, content_type="application/json", binary=False, maximum=None):
        if self.closed:
            raise ShareError("configuration")
        request = urllib.request.Request(self.url + path, data=body, method=method,
                                         headers={"Authorization": "Bearer " + self.key,
                                                  "Accept": "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if binary else "application/json",
                                                  "Content-Type": content_type})
        maximum = maximum if maximum is not None else MAX_RESULT if binary else MAX_JSON
        try:
            with self.opener.open(request, timeout=180) as response:
                if not 200 <= response.status < 300:
                    raise ShareError("response_invalid")
                raw = response.read(maximum + 1)
                if len(raw) > maximum:
                    raise ShareError("response_invalid")
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                raise ShareError("redirect_blocked") from None
            try:
                envelope = json_object(error.read(MAX_JSON + 1))
                code = envelope.get("error", {}).get("code") if isinstance(envelope.get("error"), dict) else None
            except (ShareError, OSError, TimeoutError, http.client.HTTPException):
                code = None
            finally:
                error.close()
            raise ShareError(code if isinstance(code, str) and code in PUBLIC_CODES else "service_unavailable") from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
            raise ShareError("network_unknown") from None
        if binary:
            return raw
        envelope = json_object(raw)
        if envelope.get("ok") is not True or not isinstance(envelope.get("data"), dict):
            error = envelope.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise ShareError(code if isinstance(code, str) and code in PUBLIC_CODES else "response_invalid")
        return envelope["data"]

    def platforms(self):
        data = self._request("GET", "/share/platforms")
        platforms = data.get("platforms")
        if not isinstance(platforms, list) or len(platforms) > 200:
            raise ShareError("response_invalid")
        result = []
        for item in platforms:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ShareError("response_invalid")
            identity = item.get("id")
            if not isinstance(identity, (str, int)) or isinstance(identity, bool):
                raise ShareError("response_invalid")
            languages = item.get("languages")
            if not isinstance(languages, list) or not languages or any(language not in ("CN", "EN") for language in languages):
                raise ShareError("response_invalid")
            result.append({"id": str(identity).replace(self.key, "[redacted]")[:128],
                           "name": item["name"].replace(self.key, "[redacted]")[:128], "languages": languages})
        return {"platforms": result, "mode": "双降", "feature": "文件改写 Pro"}

    def _metadata(self, data, expected):
        job_id = data.get("job_id", expected)
        if job_id != expected:
            raise ShareError("response_invalid", expected)
        status = data.get("status")
        if status not in ("queued", "submitted", "accepted", "uploading", "committing", "processing", "completed",
                          "succeeded", "failed", "unknown", "unconfirmed", "pending", "running", "not_started", "poll_timeout"):
            raise ShareError("response_invalid", expected)
        result = {"job_id": expected, "status": status, "mode": "双降", "feature": "文件改写 Pro"}
        for field in ("platform", "language", "filename", "request_id"):
            value = data.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                result[field] = str(value).replace(self.key, "[redacted]")[:256]
        error = data.get("error")
        error_code = error.get("code") if isinstance(error, dict) else error if isinstance(error, str) else None
        if error_code:
            public_code = error_code if isinstance(error_code, str) and error_code in PUBLIC_CODES else "service_unavailable"
            result["error"] = {"code": public_code, "message": MESSAGES.get(public_code, "共享服务未完成任务，请检查状态。")}
        if isinstance(data.get("retry_allowed"), bool):
            result["retry_allowed"] = data["retry_allowed"]
        protection = data.get("content_protection")
        if isinstance(protection, dict):
            clean = {}
            for name in ("total_paragraphs", "changed_paragraphs", "accepted_paragraphs", "protected_paragraphs", "restored_paragraphs", "rejected_paragraphs"):
                value = protection.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000:
                    clean[name] = value
            if protection.get("status") in ("protected", "blocked"):
                clean["status"] = protection["status"]
            if protection.get("version") == "1":
                clean["version"] = protection["version"]
            if isinstance(protection.get("review_required"), bool):
                clean["review_required"] = protection["review_required"]
            result["content_protection"] = clean
        if data.get("quality_status") in ("review_required", "limited_rewrite", "blocked", "unverified"):
            result["quality_status"] = data["quality_status"]
        return result

    def job_status(self, job_id):
        job_id = safe_id(job_id)
        return self._metadata(self._request("GET", "/share/jobs/" + job_id), job_id)

    def rewrite_file(self, path, platform=None, language="CN", request_id=None, retry_not_started=False):
        if not isinstance(retry_not_started, bool):
            raise ShareError("invalid_input")
        if platform is None or platform == "":
            return {"status": "need_platform", "message": MESSAGES["need_platform"], **self.platforms()}
        if not isinstance(platform, str) or language not in ("CN", "EN"):
            raise ShareError("invalid_input")
        catalog = self.platforms()["platforms"]
        chosen = next((item for item in catalog if platform in (item["id"], item["name"])), None)
        if chosen is None or language not in chosen["languages"]:
            raise ShareError("invalid_platform")
        try:
            if not isinstance(path, str) or not path:
                raise ShareError("invalid_input")
            file = Path(path).expanduser().resolve()
            if file.suffix.lower() not in (".doc", ".docx") or not file.is_file():
                raise ShareError("invalid_file")
            if file.suffix.lower() == ".doc":
                raise ShareError("docx_required")
            if file.stat().st_size > MAX_FILE:
                raise ShareError("file_too_large")
            with file.open("rb") as input_file:
                raw = input_file.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise ShareError("file_too_large")
            if not raw or (file.suffix.lower() == ".docx" and not is_docx(raw)) or (
                    file.suffix.lower() == ".doc" and not raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))):
                raise ShareError("invalid_file")
        except OSError:
            raise ShareError("local_file") from None
        fingerprint = hashlib.sha256(raw + chosen["id"].encode() + b"\0" + language.encode()).hexdigest()
        owner_state = self.state_dir / self.namespace
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            protect_private_path(self.state_dir, directory=True)
        except OSError:
            raise ShareError("local_file") from None
        index_path = owner_state / ("fingerprint-" + fingerprint + ".json")
        if request_id is None and index_path.exists():
            try:
                request_id = json_object(index_path.read_bytes())["job_id"]
            except (OSError, KeyError):
                raise ShareError("local_file") from None
        job_id = safe_id(request_id) if request_id is not None else uuid.uuid4().hex
        receipt_path = owner_state / (job_id + ".json")
        if receipt_path.exists():
            try:
                receipt = json_object(receipt_path.read_bytes())
            except OSError:
                raise ShareError("local_file") from None
            if receipt.get("fingerprint") != fingerprint:
                raise ShareError("request_id_conflict", job_id)
            try:
                prior = self.job_status(job_id)
            except ShareError as error:
                error.job_id = job_id
                raise
            if not retry_not_started:
                return prior
            if prior["status"] != "not_started" or prior.get("retry_allowed") is not True:
                raise ShareError("request_already_attempted", job_id)
        elif retry_not_started:
            raise ShareError("request_already_attempted", job_id)
        receipt = {"job_id": job_id, "fingerprint": fingerprint, "source_path": str(file),
                   "platform": chosen["id"], "language": language, "status": "attempted"}
        try:
            private_write(receipt_path, receipt)
        except ShareError as error:
            error.job_id = job_id
            raise
        private_write(index_path, {"job_id": job_id})
        query = urllib.parse.urlencode({"filename": file.name, "platform": chosen["id"], "language": language, "request_id": job_id})
        mime = "application/msword" if file.suffix.lower() == ".doc" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        try:
            result = self._metadata(self._request("POST", "/share/files?" + query, raw, mime), job_id)
        except ShareError as error:
            error.job_id = job_id
            raise
        receipt["status"] = result["status"]
        try:
            private_write(receipt_path, receipt)
        except ShareError as error:
            error.job_id = job_id
            raise
        return result

    def get_result(self, job_id, output_dir):
        job_id = safe_id(job_id)
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise ShareError("invalid_input")
        status = self.job_status(job_id)
        if (status["status"] == "failed" and isinstance(status.get("error"), dict)
                and status["error"].get("code") == "document_integrity"):
            raise ShareError("document_integrity", job_id)
        if status["status"] not in ("completed", "succeeded"):
            raise ShareError("job_not_ready", job_id)
        raw = self._request("GET", "/share/jobs/" + job_id + "/result", binary=True)
        suffix = ".docx" if is_docx(raw) else ".doc" if raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1")) else None
        if suffix is None:
            raise ShareError("response_invalid", job_id)
        try:
            directory = Path(output_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            file = directory / (job_id + "-改写" + suffix)
            if file.exists():
                file = directory / (job_id + "-改写-" + uuid.uuid4().hex[:8] + suffix)
            descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            if IS_WINDOWS:
                try:
                    protect_private_path(file)
                except ShareError:
                    os.close(descriptor)
                    file.unlink()
                    raise
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            raise ShareError("local_file", job_id) from None
        result = {"job_id": job_id, "status": "downloaded", "path": str(file), "bytes": len(raw), "mode": "双降", "feature": "文件改写 Pro"}
        for name in ("content_protection", "quality_status"):
            if name in status:
                result[name] = status[name]
        return result

    def close(self):
        self.closed = True
        self.key = ""

    def _literature_metadata(self, data):
        allowed = {"results", "items", "candidates", "citations", "provenance", "title", "authors", "source", "date",
                   "fileid", "dbname", "cite", "doi", "abstract", "engine", "query", "retrieved_at", "keyword",
                   "total", "count", "cached", "page", "size", "input", "matched", "evidence_level", "status", "operation", "job_id",
                   "verified_count", "mismatch_count", "unverified_count", "verified", "confidence", "matched_title", "standard_cite", "note", "reason",
                   "verification_method", "mismatched_fields", "full_text_read",
                   "downloaded_count", "failed_count", "failed", "bytes", "result_available", "error", "code", "message"}
        def public_error(value):
            if value is None:
                return None
            code = value.get("code") if isinstance(value, dict) else value
            error = ShareError(code if isinstance(code, str) and code in PUBLIC_CODES else "service_unavailable")
            return {"code": error.code, "message": str(error)}
        def clean(value, depth=0):
            if depth > 8:
                raise ShareError("response_invalid")
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key not in allowed:
                        continue
                    if key == "error":
                        result[key] = public_error(item)
                    elif key == "doi" and isinstance(item, str):
                        # Keep a DOI identifier while suppressing arbitrary URLs.
                        identifier = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", item.strip())
                        result[key] = clean(identifier, depth + 1)
                    elif key == "verification_method":
                        if item not in ("doi_exact", "fuzzy_candidate", "unavailable"):
                            raise ShareError("response_invalid")
                        result[key] = item
                    elif key == "mismatched_fields":
                        if not isinstance(item, list) or any(field not in ("title", "authors", "source", "year") for field in item):
                            raise ShareError("response_invalid")
                        result[key] = item
                    elif key == "full_text_read":
                        if item is not False:
                            raise ShareError("response_invalid")
                        result[key] = False
                    elif key == "failed":
                        if not isinstance(item, list) or len(item) > 200 or any(not isinstance(entry, dict) for entry in item):
                            raise ShareError("response_invalid")
                        # Failed rows accept identifiers and fixed error codes,
                        # never upstream reason strings or response bodies.
                        result[key] = [clean({field: entry[field] for field in ("fileid", "title", "error") if field in entry}, depth + 1) for entry in item]
                    else:
                        result[key] = clean(item, depth + 1)
                return result
            if isinstance(value, list):
                if len(value) > 200:
                    raise ShareError("response_invalid")
                return [clean(item, depth + 1) for item in value]
            if isinstance(value, str):
                return re.sub(r"https?://\S+", "[链接已省略]", value.replace(self.key, "[redacted]"))[:4000]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            raise ShareError("response_invalid")
        return clean(data)

    def _literature_read(self, action, values):
        raw = json.dumps(values, ensure_ascii=False).encode("utf-8")
        return self._literature_metadata(self._request("POST", "/share/literature/" + action, raw))

    def literature_search(self, keyword, page=1, size=10):
        return self._literature_read("search", {"keyword": keyword, "page": page, "size": size})

    def literature_search_en(self, query, size=10, source="crossref"):
        return self._literature_read("search-en", {"query": query, "size": size, "source": source})

    def literature_verify(self, citations):
        return self._literature_read("verify", {"citations": citations})

    def _literature_submit(self, operation, values, request_id):
        fingerprint = hashlib.sha256(json.dumps({"operation": operation, **values}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        owner = self.state_dir / self.namespace
        index = owner / ("literature-fingerprint-" + fingerprint + ".json")
        if request_id is None and index.exists():
            try:
                request_id = json_object(index.read_bytes())["job_id"]
            except (OSError, KeyError):
                raise ShareError("local_file") from None
        job_id = safe_id(request_id) if request_id is not None else uuid.uuid4().hex
        receipt = owner / ("literature-" + job_id + ".json")
        if receipt.exists():
            try:
                prior = json_object(receipt.read_bytes())
            except OSError:
                raise ShareError("local_file") from None
            if prior.get("fingerprint") != fingerprint:
                raise ShareError("request_id_conflict", job_id)
            return self.literature_job_status(job_id)
        private_write(receipt, {"job_id": job_id, "fingerprint": fingerprint, "operation": operation})
        private_write(index, {"job_id": job_id})
        try:
            result = self._literature_read(operation, {**values, "request_id": job_id})
            if result.get("job_id") != job_id or result.get("status") not in ("queued", "running", "succeeded", "partial", "failed", "interrupted"):
                raise ShareError("response_invalid", job_id)
            return result
        except ShareError as error:
            error.job_id = job_id
            raise

    def literature_fetch(self, keyword, top=3, request_id=None):
        return self._literature_submit("fetch", {"keyword": keyword, "top": top}, request_id)

    def literature_download(self, keyword, fileid, page=1, request_id=None):
        return self._literature_submit("download", {"keyword": keyword, "fileid": fileid, "page": page}, request_id)

    def literature_job_status(self, job_id):
        job_id = safe_id(job_id)
        result = self._literature_metadata(self._request("GET", "/share/literature/jobs/" + job_id))
        if result.get("job_id") != job_id or result.get("status") not in ("queued", "running", "succeeded", "partial", "failed", "interrupted"):
            raise ShareError("response_invalid", job_id)
        return result

    def literature_get_result(self, job_id, output_dir):
        job_id = safe_id(job_id)
        status = self.literature_job_status(job_id)
        if status.get("result_available") is not True:
            raise ShareError("job_not_ready", job_id)
        raw = self._request("GET", "/share/literature/jobs/" + job_id + "/result", binary=True, maximum=MAX_ARCHIVE)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if len(archive.infolist()) > 5000 or sum(item.file_size for item in archive.infolist()) > 500 * 1024 * 1024:
                    raise ValueError()
            directory = Path(output_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (job_id + "-文献-" + uuid.uuid4().hex[:8] + ".zip")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            if IS_WINDOWS:
                try:
                    protect_private_path(path)
                except ShareError:
                    os.close(descriptor)
                    path.unlink()
                    raise
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
        except (zipfile.BadZipFile, ValueError):
            raise ShareError("response_invalid", job_id) from None
        except OSError:
            raise ShareError("local_file", job_id) from None
        return {"job_id": job_id, "status": "downloaded", "path": str(path), "bytes": len(raw)}

    def service_capacity(self):
        data = self._request("GET", "/share/literature/capacity")
        quota = self._request("GET", "/share/literature/quota")
        allowed = {"date", "timezone", "lanes", "daily_limit", "used", "remaining", "download_concurrency",
                   "search_concurrency", "owner_search_concurrency", "cache_seconds", "running", "queued",
                   "literature", "file_rewrite", "global", "owner", "limit", "available", "scope", "quota",
                   "search_per_owner", "queue_limit", "queue_per_owner", "max_top", "lane_count", "daily_attempt_limit",
                   "per_lane_attempt_limit", "configured_limits_only", "day", "used_attempts", "reserved_attempts", "remaining_attempts",
                   "configured_lane_count", "active_lane_count", "needs_session_renewal", "rewrite_file", "concurrency",
                   "max_input_bytes", "max_result_bytes", "defaultMode", "feature", "http_requests", "global_limit", "per_ip_limit",
                   "unknown_lane_count", "own_pending_jobs", "own_reserved_attempts", "successful_downloads_today", "completed_jobs_today",
                   "verified_active_remaining_attempts", "unknown_remaining_attempts", "unavailable_lane_count", "downloads_enabled"}
        def clean(value):
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items() if key in allowed}
            if isinstance(value, (int, bool)) or value is None:
                return value
            if isinstance(value, str):
                return value.replace(self.key, "[redacted]")[:128]
            raise ShareError("response_invalid")
        return {"capacity": clean(data), "quota": clean(quota)}


def spec(name, description, properties=None, required=None):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties or {},
                                                                          "required": required or [], "additionalProperties": False}}


TOOLS = [
    spec("list_platforms", "列出可选检测平台。用户要求降重、降AI或双降时，必须先询问并确认本次目标平台，不得自行选择。模式固定双降，使用文件改写 Pro 功能。"),
    spec("rewrite_file", "先确认本机 DOCX 路径和本次目标检测平台；无默认平台，不得自行选择。DOC须先另存为DOCX，上传最多10MB，固定双降文件改写Pro。参考文献、数字统计段、表格公式等从原稿保留，只接收可可靠对应的普通叙述改写。返回保护计数和quality_status；limited_rewrite表示保留较多原文，不能宣称全文双降有效。缺platform仅列平台。结果未知保留job_id查询，避免另建任务。",
         {"path": {"type": "string", "description": "用户指定的本机 DOCX 文件绝对路径；DOC先另存为DOCX。"},
          "platform": {"type": "string", "description": "用户明确确认的平台名称或平台id字符串；没有默认平台。"}, "language": {"type": "string", "enum": ["CN", "EN"], "default": "CN"},
          "request_id": {"type": "string"},
          "retry_not_started": {"type": "boolean", "default": False,
                                "description": "用户明确重试且原任务为not_started、retry_allowed=true时才使用。"}}, ["path"]),
    spec("job_status", "查询原任务状态及内容保护计数。旧任务首次查询会在服务器保护重建，不重复收费提交。succeeded仅指处理完成；quality_status为review_required或limited_rewrite时仍需内容复核，不能声称检测通过。不得自行改平台重新提交。", {"job_id": {"type": "string"}}, ["job_id"]),
    spec("get_result", "保存经过保护重建的Word至本机（最多40MB），返回本机路径、保护计数和quality_status。上游原始返回由服务器单独留存。document_integrity时停止交付、不重发；limited_rewrite须告知改写范围有限。仍需内容与排版复核，不能凭下载成功证明查重/AI分数下降。不得自行改平台重新提交。",
         {"job_id": {"type": "string"}, "output_dir": {"type": "string"}}, ["job_id", "output_dir"]),
]

TOOLS.extend([
    spec("literature_search", "检索中文学术文献，返回书目信息与摘要。元数据或摘要不能当作已取得全文或研究结果。",
         {"keyword": {"type": "string", "maxLength": 200}, "page": {"type": "integer", "minimum": 1, "maximum": 50},
          "size": {"type": "integer", "minimum": 1, "maximum": 30}}, ["keyword"]),
    spec("literature_search_en", "检索英文文献书目，可选 Crossref 或 arXiv。仅据实际摘要/全文决定证据层级，不将搜索记录当作全文。",
         {"query": {"type": "string", "maxLength": 200}, "size": {"type": "integer", "minimum": 1, "maximum": 30},
          "source": {"type": "string", "enum": ["crossref", "arxiv"]}}, ["query"]),
    spec("literature_verify", "核验最多10条参考文献的书目匹配情况，输入有DOI时保留DOI并优先精确核对。mismatch可能是模糊搜索选错候选，不可直接断定用户引用错误；短题名需保守判定。匹配不等于已验证论文结论或阅读全文。",
         {"citations": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "string", "maxLength": 1000}}}, ["citations"]),
    spec("service_capacity", "只读查询检索与Word改写容量及服务状态。文献下载已关闭；downloads_enabled=false表示不会提供文献下载，历史额度不表示可下载。配置上限不是实测吞吐。")
])


def valid_argument(value, schema):
    kind = schema["type"]
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if kind == "array":
        return isinstance(value, list) and schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100) and all(valid_argument(item, schema["items"]) for item in value)
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= schema.get("maxLength", 4096)
            and ("enum" not in schema or value in schema["enum"]))


class ShareMCP:
    def __init__(self, client):
        self.client = client
        self.initialized = False

    @staticmethod
    def rpc_error(identity, code, message):
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}}

    def dispatch(self, message):
        if not isinstance(message, dict):
            return self.rpc_error(None, -32600, "Invalid Request")
        if "id" not in message:
            return None
        identity = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not (identity is None or isinstance(identity, (str, int)) and not isinstance(identity, bool)):
            return self.rpc_error(None, -32600, "Invalid Request")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self.rpc_error(identity, -32602, "Invalid parameters")
        if "_meta" in params and not isinstance(params["_meta"], dict):
            return self.rpc_error(identity, -32602, "Invalid metadata")
        if method == "initialize":
            version = params.get("protocolVersion")
            if not isinstance(version, str) or not version:
                return self.rpc_error(identity, -32602, "Invalid protocolVersion")
            self.initialized = True
            result = {"protocolVersion": version if version in VERSIONS else VERSIONS[-1], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "bixia-mcp", "version": "1.0.5"},
                      "instructions": "笔下MCP提供文献检索、书目核验与Word改写，文献下载已关闭。用户要求降重、降AI或双降时，先取得本机DOC/DOCX路径并询问本次目标检测平台，未明确不得自行选择。默认语言CN，模式固定双降，功能为文件改写Pro。保留Word任务job_id查询进度；完成后将改写Word文件保存至本机并提供路径。书目或摘要不等于全文证据，文献匹配不等于论文结论已验证。"}
        elif method == "ping":
            result = {}
        elif method not in ("tools/list", "tools/call"):
            return self.rpc_error(identity, -32601, "Method not found")
        elif not self.initialized:
            return self.rpc_error(identity, -32000, "Server has not been initialized")
        elif method == "tools/list":
            if set(params) - {"cursor", "_meta"} or params.get("cursor") not in (None, ""):
                return self.rpc_error(identity, -32602, "Invalid parameters")
            result = {"tools": TOOLS}
        else:
            name = params.get("name")
            arguments = params.get("arguments", {})
            tool = next((tool for tool in TOOLS if tool["name"] == name), None)
            if (tool is None or not isinstance(arguments, dict) or set(params) - {"name", "arguments", "_meta"}
                    or set(arguments) - set(tool["inputSchema"]["properties"])
                    or any(key not in arguments for key in tool["inputSchema"]["required"])
                    or any(not valid_argument(value, tool["inputSchema"]["properties"][key]) for key, value in arguments.items())
                    or arguments.get("language", "CN") not in ("CN", "EN")):
                return self.rpc_error(identity, -32602, "Invalid tool arguments")
            try:
                action = self.client.platforms if name == "list_platforms" else getattr(self.client, name)
                value = action(**arguments)
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except ShareError as error:
                value = {"error": {"code": error.code, "message": str(error)}}
                if error.job_id:
                    value["job_id"] = error.job_id
                result = {"isError": True, "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except Exception:
                result = {"isError": True, "content": [{"type": "text", "text": '{"error":{"code":"internal_error","message":"操作未完成，请查询原任务。"}}'}]}
        return {"jsonrpc": "2.0", "id": identity, "result": result}

    def serve(self, input_stream, output_stream):
        for line in input_stream:
            if not line.strip():
                continue
            try:
                if len(line) > MAX_JSON:
                    raise ValueError()
                response = self.dispatch(json.loads(line))
            except ValueError:
                response = self.rpc_error(None, -32700, "Parse error")
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                output_stream.flush()


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["serve", "configure"], default="serve")
    parser.add_argument("--config", help="Private config file path; keys never belong in CLI arguments")
    parser.add_argument("--state-dir", help="Private local request receipt directory")
    args = parser.parse_args()
    client = None
    try:
        if args.command == "configure":
            if not sys.stdin.isatty():
                raise ShareError("configuration")
            url = checked_url(input(f"共享服务地址 [{DEFAULT_URL}]：").strip() or DEFAULT_URL)
            key = getpass.getpass("个人访问密钥（输入不显示）：")
            ca = str(Path(input(f"可信 CA 文件 [{DEFAULT_CA}]：").strip() or str(DEFAULT_CA)).expanduser().resolve())
            client = ShareClient(url, key, ca_file=ca, state_dir=args.state_dir)
            private_write(Path(args.config or DEFAULT_CONFIG).expanduser(), {"server_url": url, "api_key": key, "ca_file": str(Path(ca).expanduser().resolve())})
            print("本机私有配置已保存，尚未提交文件。")
            return 0
        ensure_auto_config(args.config)
        client = ShareClient.from_config(args.config, args.state_dir,
                                        allow_missing="BIXIA_MCP_KEY" in os.environ or "ACADEMIC_REWRITE_KEY" in os.environ)
        ShareMCP(client).serve(sys.stdin, sys.stdout)
        return 0
    except KeyboardInterrupt:
        return 0
    except ShareError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print(MESSAGES["configuration"], file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
