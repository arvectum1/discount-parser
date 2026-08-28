from __future__ import annotations

from typing import Protocol

from .models import AcquisitionRequest, PageSnapshot


class HTTPTransport(Protocol):
    name: str

    def fetch(self, request: AcquisitionRequest) -> PageSnapshot: ...


class PageRenderer(Protocol):
    name: str

    def render(self, request: AcquisitionRequest) -> PageSnapshot: ...
