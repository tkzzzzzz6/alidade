import io

import pytest

from alidade import _startup_spinner


class CP1252TTY(io.StringIO):
    encoding = "cp1252"

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        value.encode(self.encoding)
        return super().write(value)


@pytest.mark.parametrize("raises", [False, True])
def test_spinner_output_is_cp1252_safe(monkeypatch, raises):
    stream = CP1252TTY()
    monkeypatch.setattr(_startup_spinner.sys, "stdout", stream)
    monkeypatch.setattr(_startup_spinner, "_FRAME_INTERVAL", 0.001)

    if raises:
        with pytest.raises(RuntimeError):
            with _startup_spinner.spinner("Loading test"):
                raise RuntimeError("expected")
        assert "ERROR" in stream.getvalue()
    else:
        with _startup_spinner.spinner("Loading test"):
            pass
        assert "OK" in stream.getvalue()

    stream.getvalue().encode("cp1252")

