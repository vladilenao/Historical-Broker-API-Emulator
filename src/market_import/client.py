"""HTTP-клиент экспортного журнала My Robot."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class ExportRequestError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class MarketExportClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Адрес экспорта должен начинаться с http:// или https://")
        if not token:
            raise ValueError("Для экспорта нужен Bearer-токен")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                problem = json.load(exc)
            except (ValueError, OSError):
                problem = {}
            raise ExportRequestError(
                str(problem.get("message", f"HTTP {exc.code} от My Robot")),
                exc.code,
                problem.get("code"),
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ExportRequestError(f"My Robot недоступен: {exc}") from exc
        except ValueError as exc:
            raise ExportRequestError("My Robot вернул некорректный JSON") from exc
        if not isinstance(result, dict):
            raise ExportRequestError("My Robot вернул не объект JSON")
        return result

    def source(self) -> dict:
        return self._request("GET", "/api/v1/market-data/source")

    def changes(self, producer_id: str, after_id: int, limit: int = 500) -> dict:
        query = urlencode({"producer_id": producer_id, "after_id": after_id, "limit": limit})
        return self._request("GET", f"/api/v1/market-data/changes?{query}")

    def checkpoint(self, consumer_id: str, producer_id: str, change_id: int) -> dict:
        path = f"/api/v1/market-data/consumers/{quote(consumer_id, safe='')}/checkpoint"
        return self._request("PUT", path, {"producer_id": producer_id, "change_id": change_id})

    def trading_schedules(self, producer_id: str, after_id: int, limit: int = 500) -> dict:
        query = urlencode({"producer_id": producer_id, "after_id": after_id, "limit": limit})
        return self._request("GET", f"/api/v1/market-data/trading-schedules?{query}")

    def schedule_checkpoint(self, consumer_id: str, producer_id: str, change_id: int) -> dict:
        consumer = quote(consumer_id, safe="")
        path = f"/api/v1/market-data/consumers/{consumer}/trading-schedules-checkpoint"
        return self._request("PUT", path, {"producer_id": producer_id, "change_id": change_id})
