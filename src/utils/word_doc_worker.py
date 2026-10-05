"""
Worker process for utils.knesset_db._extract_doc_text: reads one legacy .doc through a fresh
Word instance of its own and writes the text to a file. Run as
    python word_doc_worker.py <doc_path> <text_output_path> <word_pid_path>
The Word PID is written as soon as Word starts, so the parent can kill exactly this instance
when the worker hangs (a dialog, a broken document).
"""

import sys
import uuid

import win32com.client
import win32gui
import win32process

WORD_MAIN_WINDOW_CLASS = "OpusApp"
WORD_ALERTS_NONE = 0


def _word_process_id(word) -> int | None:
    unique_caption = f"knesset-lm-word-{uuid.uuid4().hex}"
    word.Caption = unique_caption
    window_handle = win32gui.FindWindow(WORD_MAIN_WINDOW_CLASS, unique_caption)
    if not window_handle:
        return None
    _, process_id = win32process.GetWindowThreadProcessId(window_handle)
    return process_id


def start_isolated_word(word_pid_path: str):
    """A new Word instance (never the user's open one); its PID goes to word_pid_path."""
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = WORD_ALERTS_NONE
    try:
        word_process_id = _word_process_id(word)
    except Exception as exc:
        print(f"[word_doc_worker] finding the Word PID failed ({exc})", file=sys.stderr, flush=True)
        word_process_id = None
    if word_process_id is None:
        print("[word_doc_worker] Word PID not found; it cannot be killed on timeout", file=sys.stderr, flush=True)
    else:
        with open(word_pid_path, "w", encoding="ascii") as pid_file:
            pid_file.write(str(word_process_id))
    return word


def extract_doc_text(doc_path: str, text_output_path: str, word_pid_path: str) -> None:
    word = start_isolated_word(word_pid_path)
    com_document = None
    try:
        com_document = word.Documents.Open(doc_path, ConfirmConversions=False, ReadOnly=True,
                                           AddToRecentFiles=False, Visible=False)
        text = com_document.Content.Text
        with open(text_output_path, "w", encoding="utf-8") as text_file:
            text_file.write(text)
    finally:
        if com_document is not None:
            try:
                com_document.Close(False)
            except Exception as exc:
                print(f"[word_doc_worker] closing the document failed ({exc})", file=sys.stderr, flush=True)
        try:
            word.Quit()
        except Exception as exc:
            print(f"[word_doc_worker] quitting Word failed ({exc})", file=sys.stderr, flush=True)


if __name__ == "__main__":
    extract_doc_text(sys.argv[1], sys.argv[2], sys.argv[3])
