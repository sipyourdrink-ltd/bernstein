import subprocess


def run(cmd: str) -> None:
    subprocess.call(cmd, shell=True)
