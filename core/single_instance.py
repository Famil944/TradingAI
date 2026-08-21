import socket


class SingleInstance:
    """Keeps a localhost port open so only one polling process can run."""

    def __init__(self, port: int = 47653):
        self.port = port
        self._socket = None

    def acquire(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))
            sock.listen(1)
        except OSError:
            sock.close()
            return False
        self._socket = sock
        return True

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None

