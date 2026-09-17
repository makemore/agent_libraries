import subprocess
import sys


def test_importing_ace_does_not_import_django() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ace; assert 'django' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
