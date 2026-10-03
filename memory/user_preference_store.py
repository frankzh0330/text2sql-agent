"""User-scoped preference signals: a bounded candidate bias applied after recall, before the accept/confirm decision."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_FIELD_TO_BUCKET = {
    "table": "tables",
    "metric": "metrics",
    "column": "columns",
    "group_by": "columns",
}
_MAX_BIAS_SCORE = 6.0


class UserPreferenceStore:
    """Append-light JSON preference storage scoped by project_id + user_id."""

    def __init__(self, data_path: str = "data/user_preferences"):
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        # load → increment → save 是读改写序列，并发请求下需串行
        self._write_lock = threading.Lock()

    def record_selection(
        self,
        project_id: int,
        user_id: str | None,
        *,
        table: str | None = None,
        metric: str | None = None,
        columns: list[str] | None = None,
    ) -> None:
        if not user_id:
            return

        if not (table or metric or columns):
            return

        with self._write_lock:
            data = self._load(project_id, user_id)
            if table:
                self._increment(data["tables"], table)
            if metric:
                self._increment(data["metrics"], metric)
            for column in columns or []:
                self._increment(data["columns"], column)
            self._save(project_id, user_id, data)

    def get_bucket(self, project_id: int, user_id: str | None, field_name: str) -> Dict[str, int]:
        if not user_id:
            return {}
        bucket = _FIELD_TO_BUCKET.get(field_name)
        if not bucket:
            return {}
        data = self._load(project_id, user_id)
        return data.get(bucket, {})

    def rerank_candidates(
        self,
        project_id: int,
        user_id: str | None,
        field_name: str,
        candidates: list[Dict[str, Any]],
    ) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
        if not user_id or not candidates:
            return candidates, {"applied": False, "reason": "no_user_or_candidates"}

        bucket = self.get_bucket(project_id, user_id, field_name)
        if not bucket:
            return candidates, {"applied": False, "reason": "no_preference_data"}

        reranked = []
        for candidate in candidates:
            raw_score = float(candidate.get("score", 0.0))
            value = candidate.get("value")
            count = int(bucket.get(value, 0))
            bias = min(_MAX_BIAS_SCORE, round(math.log2(count + 1) * 2.0, 2)) if count > 0 else 0.0
            reranked.append(
                {
                    **candidate,
                    "raw_score": raw_score,
                    "preference_count": count,
                    "preference_bias": bias,
                    "score": round(raw_score + bias, 2),
                }
            )

        reranked.sort(
            key=lambda item: (float(item.get("score", 0.0)), float(item.get("raw_score", 0.0))),
            reverse=True,
        )
        return reranked, {
            "applied": True,
            "field": field_name,
            "top_preference_hits": [
                {
                    "value": item["value"],
                    "count": item["preference_count"],
                    "bias": item["preference_bias"],
                }
                for item in reranked[:3]
                if item.get("preference_count", 0) > 0
            ],
        }

    def _path(self, project_id: int, user_id: str) -> Path:
        safe_user = user_id.replace("/", "_")
        return self.data_path / f"project_{project_id}__user_{safe_user}.json"

    def _load(self, project_id: int, user_id: str) -> Dict[str, Dict[str, int]]:
        path = self._path(project_id, user_id)
        if not path.exists():
            return {"tables": {}, "metrics": {}, "columns": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to load user preferences: project=%s user=%s", project_id, user_id)
            return {"tables": {}, "metrics": {}, "columns": {}}

    def _save(self, project_id: int, user_id: str, data: Dict[str, Dict[str, int]]) -> None:
        # 先写临时文件再原子替换，避免读到半截 JSON
        path = self._path(project_id, user_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _increment(bucket: Dict[str, int], key: str) -> None:
        bucket[key] = int(bucket.get(key, 0)) + 1
