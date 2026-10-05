"""Legacy .doc extraction through a Word worker process with a timeout (utils.knesset_db._extract_doc_text)."""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
import utils.knesset_db as kdb

DOCUMENT_URL = "https://fs.knesset.gov.il/25/Committees/example.doc"


def _hanging_worker_command(doc_path: str, text_path: str, word_pid_path: str) -> list[str]:
    hang_forever = (
        "import os, sys, time\n"
        "open(sys.argv[3], 'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n"
    )
    return [sys.executable, "-c", hang_forever, doc_path, text_path, word_pid_path]


@pytest.fixture
def hanging_worker(monkeypatch):
    killed_word_pids: list[int] = []
    worker_doc_paths: list[str] = []

    def recording_command(doc_path, text_path, word_pid_path):
        worker_doc_paths.append(doc_path)
        return _hanging_worker_command(doc_path, text_path, word_pid_path)

    monkeypatch.setattr(kdb, "_WORD_COM_AVAILABLE", True)
    monkeypatch.setattr(config, "WORD_EXTRACTION_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(kdb, "_word_worker_command", recording_command)
    monkeypatch.setattr(kdb, "_kill_word_instance", killed_word_pids.append)
    return killed_word_pids, worker_doc_paths


def test_hanging_worker_times_out_and_raises(hanging_worker, capsys):
    killed_word_pids, _ = hanging_worker
    started = time.monotonic()
    with pytest.raises(kdb.WordExtractionError):
        kdb._extract_doc_text(b"not really a doc", DOCUMENT_URL)
    assert time.monotonic() - started < 30
    assert len(killed_word_pids) == 1
    assert DOCUMENT_URL in capsys.readouterr().out


def test_each_extraction_uses_its_own_temp_file(hanging_worker):
    _, worker_doc_paths = hanging_worker
    for _ in range(2):
        with pytest.raises(kdb.WordExtractionError):
            kdb._extract_doc_text(b"x", DOCUMENT_URL)
    assert len(set(worker_doc_paths)) == 2
    assert not any(Path(path).exists() for path in worker_doc_paths)


def test_session_protocol_text_raises_when_word_extraction_failed(monkeypatch):
    monkeypatch.setattr(kdb, "_get_session_protocol_documents", lambda session_id: [
        {"doc_id": 1, "name": "פרוטוקול", "format": "doc", "url": DOCUMENT_URL}])

    def timing_out_download(url, application_format):
        raise kdb.WordExtractionError(f"timed out: {url}")

    monkeypatch.setattr(kdb, "_download_document_text", timing_out_download)
    with pytest.raises(kdb.WordExtractionError):
        kdb._get_session_protocol_text(123)


def test_kill_word_instance_refuses_a_process_that_is_not_word():
    if not kdb._WORD_COM_AVAILABLE:
        pytest.skip("pywin32 is not installed")
    not_word = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        kdb._kill_word_instance(not_word.pid)
        assert not_word.poll() is None
    finally:
        not_word.kill()
        not_word.wait()


def _word_is_installed() -> bool:
    if not kdb._WORD_COM_AVAILABLE:
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
        return True
    except OSError as exc:
        print(f"Word.Application is not registered ({exc})")
        return False


_MAKE_SAMPLE_DOC = """
import sys
sys.path.insert(0, sys.argv[3])
from utils.word_doc_worker import start_isolated_word
word = start_isolated_word(sys.argv[2])
document = word.Documents.Add()
document.Content.Text = "Knesset sample protocol"
document.SaveAs2(sys.argv[1], FileFormat=0)
document.Close(False)
word.Quit()
"""


@pytest.mark.word
@pytest.mark.skipif(not _word_is_installed(), reason="Microsoft Word through pywin32 is not available")
def test_real_doc_extraction(tmp_path):
    sample_doc_path = tmp_path / "sample.doc"
    sample_word_pid_path = tmp_path / "sample.wordpid"
    src_dir = str(Path(__file__).parent.parent / "src")
    try:
        subprocess.run([sys.executable, "-c", _MAKE_SAMPLE_DOC, str(sample_doc_path), str(sample_word_pid_path), src_dir],
                       check=True, timeout=config.WORD_EXTRACTION_TIMEOUT_SECONDS, capture_output=True)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        if sample_word_pid_path.exists():
            kdb._kill_word_instance(int(sample_word_pid_path.read_text()))
        pytest.skip(f"Word could not create a sample .doc ({exc})")
    try:
        text = kdb._extract_doc_text(sample_doc_path.read_bytes(), str(sample_doc_path))
    except kdb.WordExtractionError as exc:
        pytest.skip(f"Word is blocked on this machine ({exc})")
    assert "Knesset sample protocol" in text
