from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from time import monotonic
from typing import Any, Literal

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field, ValidationError, model_validator

from core.contracts import StrictModel
from core.errors import (
    AgentOperatingUnitMismatch,
    ContractInvalid,
    ModeAgentUnavailable,
)
from core.operating_unit import OperatingUnitBinding

GET_CURRENT_OPERATING_MODE_TOOL = "get_current_operating_mode"
GET_CURRENT_OPERATING_MODES_TOOL = "get_current_operating_modes"
GET_EVALUATE_TOOL = "get_evaluate"
START_OPERATING_MODE_EVALUATION_JOB_TOOL = "start_operating_mode_evaluation_job"
GET_OPERATING_MODE_EVALUATION_JOB_TOOL = "get_operating_mode_evaluation_job"

logger = logging.getLogger(__name__)

ModeAgentModeCode = Literal[
    "NEW_PRODUCT_VALIDATION",
    "IMMEDIATE_EXIT",
    "CONTROLLED_CLEARANCE",
    "TIME_BOXED_REPAIR",
    "STABLE_OPERATION",
    "ACTIVE_ADVANCE",
    "PROFIT_HARVEST",
]


class CurrentOperatingMode(StrictModel):
    shop_id: str
    parent_asin: str
    parent_seller_sku: str
    decision_status: Literal["DECIDED", "BLOCKED", "NEEDS_HUMAN_REVIEW"]
    recommended_mode_code: ModeAgentModeCode | None
    recommended_mode: str | None
    explanation: str
    current_sale_cash_recovery: float | None = None
    parent_unit_contribution: float | None = None
    currency: str | None = None
    observation_requirements: list[Any] | None = None
    rule_version: str | None = None
    producer_version: str | None = None
    generated_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_decided_mode(self) -> CurrentOperatingMode:
        if self.decision_status == "DECIDED" and self.recommended_mode_code is None:
            raise ValueError("DECIDED operating mode requires recommended_mode_code")
        return self


class CurrentOperatingModeResult(StrictModel):
    result: CurrentOperatingMode | None


class OperatingModeKey(StrictModel):
    shop_id: str
    parent_asin: str
    parent_seller_sku: str

    @model_validator(mode="after")
    def _require_complete_key(self) -> OperatingModeKey:
        if not self.shop_id or not self.parent_asin or not self.parent_seller_sku:
            raise ValueError("operating unit key must be complete")
        return self


class CurrentOperatingModesResult(StrictModel):
    requested_count: int
    found_count: int
    missing_count: int
    results: list[CurrentOperatingMode]
    missing_operating_units: list[OperatingModeKey]


class OperatingModeEvaluationFailure(OperatingModeKey):
    error_code: str
    message: str


class OperatingModeEvaluationJobStarted(StrictModel):
    job_id: str
    status: Literal["PENDING"]
    requested_count: int
    succeeded_count: int
    failed_count: int


class OperatingModeEvaluationJob(StrictModel):
    job_id: str
    status: Literal[
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "PARTIAL_SUCCESS",
        "FAILED",
    ]
    requested_count: int
    succeeded_count: int
    failed_count: int
    results: list[CurrentOperatingMode] = Field(default_factory=list)
    failures: list[OperatingModeEvaluationFailure] = Field(default_factory=list)


def _structured_content(result: Any) -> dict[str, Any]:
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    if payload.get("isError") is True or payload.get("is_error") is True:
        # 提取服务端返回的错误消息
        error_msg = "operating mode MCP rejected the requested tool"
        content = payload.get("content") or []
        if isinstance(content, list) and content:
            first_text = ""
            if isinstance(content[0], dict):
                first_text = str(content[0].get("text") or "")
            else:
                first_text = str(content[0])
            if first_text.strip():
                error_msg = first_text.strip()
        raise ContractInvalid(error_msg)
    structured = payload.get("structuredContent") or payload.get("structured_content")
    if isinstance(structured, dict):
        return structured
    raise ContractInvalid("operating mode MCP returned no structured result")


class OperatingModeMcpClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_seconds: float = 15,
        evaluation_wait_timeout_seconds: float = 180,
        evaluation_poll_interval_seconds: float = 2,
        evaluation_cooldown_seconds: float = 3600,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("operating mode MCP URL is required")
        if not token.strip():
            raise ValueError("operating mode MCP token is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if evaluation_wait_timeout_seconds <= 0:
            raise ValueError("evaluation_wait_timeout_seconds must be positive")
        if evaluation_poll_interval_seconds <= 0:
            raise ValueError("evaluation_poll_interval_seconds must be positive")
        if evaluation_cooldown_seconds <= 0:
            raise ValueError("evaluation_cooldown_seconds must be positive")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.evaluation_wait_timeout_seconds = evaluation_wait_timeout_seconds
        self.evaluation_poll_interval_seconds = evaluation_poll_interval_seconds
        self.evaluation_cooldown_seconds = evaluation_cooldown_seconds
        self._evaluation_lock = asyncio.Lock()
        self._recent_evaluations: dict[tuple[str, str, str], float] = {}
        self._evaluation_failures: dict[
            tuple[str, str, str], OperatingModeEvaluationFailure
        ] = {}
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds * 2),
        )

    @property
    def _is_rest_endpoint(self) -> bool:
        return not self.url.rstrip("/").endswith("/mcp")

    async def _get_current_operating_mode_rest(
        self,
        *,
        shop_id: str,
        parent_asin: str,
        parent_seller_sku: str,
    ) -> CurrentOperatingMode | None:
        try:
            response = await self.client.post(
                self.url,
                json={
                    "shop_id": shop_id,
                    "parent_asin": parent_asin,
                    "parent_seller_sku": parent_seller_sku,
                },
            )
        except Exception as exc:
            raise ModeAgentUnavailable("operating mode REST endpoint unavailable") from exc
        if response.status_code == 404:
            return None
        if response.is_error:
            raise ModeAgentUnavailable(
                f"operating mode REST endpoint returned HTTP {response.status_code}"
            )
        try:
            current = CurrentOperatingMode.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ContractInvalid("operating mode REST response is invalid") from exc
        received_key = (
            current.shop_id.strip(),
            current.parent_asin.strip().upper(),
            current.parent_seller_sku.strip(),
        )
        if received_key != (shop_id, parent_asin, parent_seller_sku):
            raise AgentOperatingUnitMismatch(
                "operating mode REST endpoint returned another operating unit",
                shop_id=shop_id,
                parent_asin=parent_asin,
            )
        return current

    async def aclose(self) -> None:
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()

    async def get_current_operating_mode(
        self,
        binding: OperatingUnitBinding,
    ) -> CurrentOperatingMode | None:
        if binding.parent_seller_sku is None:
            raise ContractInvalid(
                "get_current_operating_mode requires parent_seller_sku",
                operating_unit_id=binding.operating_unit_id,
            )
        return await self.get_current_operating_mode_by_key(
            shop_id=binding.shop_id,
            parent_asin=binding.parent_asin,
            parent_seller_sku=binding.parent_seller_sku,
        )

    async def get_evaluate(self, binding: OperatingUnitBinding) -> CurrentOperatingMode:
        if binding.parent_seller_sku is None:
            raise ContractInvalid("get_evaluate requires parent_seller_sku")
        shop_account = str(binding.shop_account or "").strip()
        if not shop_account:
            raise ContractInvalid("get_evaluate requires shop_account")
        return await self.get_evaluate_by_key(
            shop_id=binding.shop_id,
            shop_account=shop_account,
            site_code=binding.site_code,
            parent_asin=binding.parent_asin,
            parent_seller_sku=binding.parent_seller_sku,
        )

    async def get_evaluate_by_key(
        self,
        *,
        shop_id: int | str,
        shop_account: str,
        site_code: str,
        parent_asin: str,
        parent_seller_sku: str,
    ) -> CurrentOperatingMode:
        normalized_shop_id = str(shop_id).strip()
        normalized_shop_account = str(shop_account or "").strip()
        normalized_site_code = str(site_code or "").strip()
        normalized_parent_asin = parent_asin.strip().upper()
        normalized_parent_seller_sku = parent_seller_sku.strip()
        if (
            not normalized_shop_id
            or not normalized_shop_account
            or not normalized_site_code
            or not normalized_parent_asin
            or not normalized_parent_seller_sku
        ):
            raise ContractInvalid("get_evaluate requires a complete business key")
        response = await self._call_tool(
            GET_EVALUATE_TOOL,
            {
                "shop_id": normalized_shop_id,
                "shop_account": normalized_shop_account,
                "site_code": normalized_site_code,
                "parent_asin": normalized_parent_asin,
                "parent_seller_sku": normalized_parent_seller_sku,
            },
        )
        try:
            current = CurrentOperatingMode.model_validate(_structured_content(response))
        except ValidationError as exc:
            raise ContractInvalid("get_evaluate response is invalid") from exc
        expected_key = (
            normalized_shop_id,
            normalized_parent_asin,
            normalized_parent_seller_sku,
        )
        if self._mode_key(current) != expected_key:
            raise AgentOperatingUnitMismatch(
                "get_evaluate returned another operating unit",
                shop_id=normalized_shop_id,
                parent_asin=normalized_parent_asin,
            )
        return current

    async def get_current_operating_mode_by_key(
        self,
        *,
        shop_id: int | str,
        parent_asin: str,
        parent_seller_sku: str,
    ) -> CurrentOperatingMode | None:
        normalized_shop_id = str(shop_id).strip()
        normalized_parent_asin = parent_asin.strip().upper()
        normalized_parent_seller_sku = parent_seller_sku.strip()
        if not normalized_shop_id or not normalized_parent_asin or not normalized_parent_seller_sku:
            raise ContractInvalid("get_current_operating_mode requires a complete business key")
        arguments = {
            "shop_id": normalized_shop_id,
            "parent_asin": normalized_parent_asin,
            "parent_seller_sku": normalized_parent_seller_sku,
        }
        if self._is_rest_endpoint:
            return await self._get_current_operating_mode_rest(**arguments)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with streamable_http_client(
                    self.url,
                    http_client=self.client,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        response = await session.call_tool(
                            GET_CURRENT_OPERATING_MODE_TOOL,
                            arguments,
                        )
        except ContractInvalid:
            raise
        except Exception as exc:
            raise ModeAgentUnavailable("operating mode MCP unavailable") from exc
        try:
            current = CurrentOperatingModeResult.model_validate(
                _structured_content(response)
            ).result
        except ValidationError as exc:
            raise ContractInvalid("operating mode MCP response is invalid") from exc
        if current is None:
            return None
        received_key = (
            current.shop_id.strip(),
            current.parent_asin.strip().upper(),
            current.parent_seller_sku.strip(),
        )
        expected_key = (
            normalized_shop_id,
            normalized_parent_asin,
            normalized_parent_seller_sku,
        )
        if received_key != expected_key:
            raise AgentOperatingUnitMismatch(
                "operating mode MCP returned another operating unit",
                shop_id=normalized_shop_id,
                parent_asin=normalized_parent_asin,
            )
        return current

    async def get_current_operating_modes(
        self,
        operating_units: list[dict[str, Any]],
    ) -> list[CurrentOperatingMode | None]:
        requested = self._validate_operating_units(
            operating_units,
            tool_name=GET_CURRENT_OPERATING_MODES_TOOL,
            maximum=10_000,
        )
        requested_keys = [self._mode_key(item) for item in requested]
        if self._is_rest_endpoint:
            return await asyncio.gather(
                *(
                    self._get_current_operating_mode_rest(
                        shop_id=item.shop_id,
                        parent_asin=item.parent_asin,
                        parent_seller_sku=item.parent_seller_sku,
                    )
                    for item in requested
                )
            )
        arguments = {
            "operating_units": [item.model_dump(mode="json") for item in requested]
        }
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with streamable_http_client(
                    self.url,
                    http_client=self.client,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        response = await session.call_tool(
                            GET_CURRENT_OPERATING_MODES_TOOL,
                            arguments,
                        )
        except ContractInvalid:
            raise
        except Exception as exc:
            raise ModeAgentUnavailable("operating mode MCP unavailable") from exc
        try:
            batch = CurrentOperatingModesResult.model_validate(
                _structured_content(response)
            )
        except ValidationError as exc:
            raise ContractInvalid(
                "operating mode MCP batch response is invalid"
            ) from exc
        if (
            batch.requested_count != len(requested)
            or batch.found_count != len(batch.results)
            or batch.missing_count != len(batch.missing_operating_units)
            or batch.found_count + batch.missing_count != batch.requested_count
        ):
            raise ContractInvalid("operating mode MCP batch counts are inconsistent")
        expected = set(requested_keys)
        found = {self._mode_key(item): item for item in batch.results}
        missing = {self._mode_key(item) for item in batch.missing_operating_units}
        if len(found) != len(batch.results) or len(missing) != len(
            batch.missing_operating_units
        ):
            raise ContractInvalid("operating mode MCP batch contains duplicate keys")
        if set(found) & missing or set(found) | missing != expected:
            raise AgentOperatingUnitMismatch(
                "operating mode MCP batch returned mismatched business keys"
            )
        return [found.get(key) for key in requested_keys]

    async def start_operating_mode_evaluation_job(
        self,
        operating_units: list[dict[str, Any]],
    ) -> OperatingModeEvaluationJobStarted:
        requested = self._validate_operating_units(
            operating_units,
            tool_name=START_OPERATING_MODE_EVALUATION_JOB_TOOL,
            maximum=100,
        )
        response = await self._call_tool(
            START_OPERATING_MODE_EVALUATION_JOB_TOOL,
            {"operating_units": [item.model_dump(mode="json") for item in requested]},
        )
        try:
            job = OperatingModeEvaluationJobStarted.model_validate(
                _structured_content(response)
            )
        except ValidationError as exc:
            raise ContractInvalid(
                "operating mode evaluation job response is invalid"
            ) from exc
        if (
            not job.job_id.strip()
            or job.requested_count != len(requested)
            or job.succeeded_count != 0
            or job.failed_count != 0
        ):
            raise ContractInvalid(
                "operating mode evaluation job creation counts are inconsistent"
            )
        return job

    async def get_operating_mode_evaluation_job(
        self,
        job_id: str,
    ) -> OperatingModeEvaluationJob:
        normalized_job_id = job_id.strip()
        if not normalized_job_id:
            raise ContractInvalid("operating mode evaluation job_id is required")
        response = await self._call_tool(
            GET_OPERATING_MODE_EVALUATION_JOB_TOOL,
            {"job_id": normalized_job_id},
        )
        try:
            job = OperatingModeEvaluationJob.model_validate(
                _structured_content(response)
            )
        except ValidationError as exc:
            raise ContractInvalid(
                "operating mode evaluation job status response is invalid"
            ) from exc
        if job.job_id.strip() != normalized_job_id:
            raise ContractInvalid("operating mode evaluation job_id mismatch")
        if (
            job.requested_count < 1
            or job.succeeded_count != len(job.results)
            or job.failed_count != len(job.failures)
            or job.succeeded_count + job.failed_count > job.requested_count
        ):
            raise ContractInvalid(
                "operating mode evaluation job status counts are inconsistent"
            )
        result_keys = [self._mode_key(item) for item in job.results]
        failure_keys = [self._mode_key(item) for item in job.failures]
        if len(result_keys) != len(set(result_keys)) or len(failure_keys) != len(set(failure_keys)):
            raise ContractInvalid(
                "operating mode evaluation job status contains duplicate business keys"
            )
        if set(result_keys) & set(failure_keys):
            raise ContractInvalid(
                "operating mode evaluation job status overlaps success and failure keys"
            )
        terminal = job.status in {"SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"}
        if terminal and job.succeeded_count + job.failed_count != job.requested_count:
            raise ContractInvalid(
                "terminal operating mode evaluation job counts are incomplete"
            )
        return job

    async def get_or_evaluate_operating_modes(
        self,
        operating_units: list[dict[str, Any]],
    ) -> list[CurrentOperatingMode | None]:
        async with self._evaluation_lock:
            requested = self._validate_operating_units(
                operating_units,
                tool_name=GET_CURRENT_OPERATING_MODES_TOOL,
                maximum=10_000,
            )
            normalized_units = [item.model_dump(mode="json") for item in requested]
            current_modes = await self.get_current_operating_modes(normalized_units)
            missing_units = [
                normalized_units[index]
                for index, current in enumerate(current_modes)
                if current is None
            ]
            if not missing_units:
                return current_modes

            now = monotonic()
            self._recent_evaluations = {
                key: expires_at
                for key, expires_at in self._recent_evaluations.items()
                if expires_at > now
            }
            eligible_units = [
                item
                for item in missing_units
                if self._mode_key(OperatingModeKey.model_validate(item))
                not in self._recent_evaluations
            ]
            if not eligible_units:
                return current_modes

            jobs: list[tuple[OperatingModeEvaluationJobStarted, list[dict[str, Any]]]] = []
            for start in range(0, len(eligible_units), 100):
                chunk = eligible_units[start : start + 100]
                job = await self.start_operating_mode_evaluation_job(chunk)
                jobs.append((job, chunk))
                expires_at = monotonic() + self.evaluation_cooldown_seconds
                for item in chunk:
                    key = self._mode_key(OperatingModeKey.model_validate(item))
                    self._recent_evaluations[key] = expires_at
            if len({job.job_id for job, _ in jobs}) != len(jobs):
                raise ContractInvalid(
                    "operating mode evaluation returned duplicate job IDs"
                )
            completed_jobs = await asyncio.gather(
                *(
                    self._wait_for_evaluation_job(job.job_id, expected_units=chunk)
                    for job, chunk in jobs
                )
            )
            for job in completed_jobs:
                if job.failed_count:
                    logger.warning(
                        "operating mode evaluation job completed with failures: "
                        "job_id=%s status=%s requested=%s succeeded=%s failed=%s",
                        job.job_id,
                        job.status,
                        job.requested_count,
                        job.succeeded_count,
                        job.failed_count,
                    )
                for failure in job.failures:
                    self._evaluation_failures[self._mode_key(failure)] = failure

            refreshed_modes = await self.get_current_operating_modes(missing_units)
            completed_results = {
                self._mode_key(result): result
                for job in completed_jobs
                for result in job.results
            }
            refreshed_modes = [
                current
                or completed_results.get(
                    self._mode_key(OperatingModeKey.model_validate(item))
                )
                for item, current in zip(
                    missing_units,
                    refreshed_modes,
                    strict=True,
                )
            ]
            refreshed = iter(refreshed_modes)
            return [
                next(refreshed) if current is None else current
                for current in current_modes
            ]

    def take_evaluation_failures(
        self,
        keys: set[tuple[str, str, str]] | None = None,
    ) -> dict[tuple[str, str, str], OperatingModeEvaluationFailure]:
        """Return and clear recorded per-unit evaluation failures.

        When ``keys`` is provided, only matching failures are removed, so
        callers that process the unit list in parts can drain each part once.
        """
        if keys is None:
            failures = dict(self._evaluation_failures)
            self._evaluation_failures.clear()
            return failures
        found = {
            key: self._evaluation_failures[key]
            for key in keys
            if key in self._evaluation_failures
        }
        for key in found:
            del self._evaluation_failures[key]
        return found

    async def _wait_for_evaluation_job(
        self,
        job_id: str,
        *,
        expected_units: list[dict[str, Any]],
    ) -> OperatingModeEvaluationJob:
        expected_keys = {
            self._mode_key(OperatingModeKey.model_validate(item))
            for item in expected_units
        }
        try:
            async with asyncio.timeout(self.evaluation_wait_timeout_seconds):
                while True:
                    job = await self.get_operating_mode_evaluation_job(job_id)
                    observed_keys = {
                        self._mode_key(item)
                        for item in [*job.results, *job.failures]
                    }
                    if not observed_keys <= expected_keys:
                        raise AgentOperatingUnitMismatch(
                            "operating mode evaluation job returned mismatched business keys"
                        )
                    if job.status in {"SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"}:
                        if observed_keys != expected_keys:
                            raise AgentOperatingUnitMismatch(
                                "terminal operating mode evaluation job omitted business keys"
                            )
                        return job
                    await asyncio.sleep(self.evaluation_poll_interval_seconds)
        except TimeoutError as exc:
            raise ModeAgentUnavailable(
                "operating mode evaluation job timed out",
                job_id=job_id,
            ) from exc

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with streamable_http_client(
                    self.url,
                    http_client=self.client,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        return await session.call_tool(tool_name, arguments)
        except ContractInvalid:
            raise
        except Exception as exc:
            raise ModeAgentUnavailable("operating mode MCP unavailable") from exc

    @classmethod
    def _validate_operating_units(
        cls,
        operating_units: list[dict[str, Any]],
        *,
        tool_name: str,
        maximum: int,
    ) -> list[OperatingModeKey]:
        if not 1 <= len(operating_units) <= maximum:
            raise ContractInvalid(
                f"{tool_name} requires between 1 and {maximum} operating units"
            )
        requested: list[OperatingModeKey] = []
        for item in operating_units:
            try:
                requested.append(OperatingModeKey.model_validate({
                    "shop_id": str(item.get("shop_id") or "").strip(),
                    "parent_asin": str(item.get("parent_asin") or "").strip().upper(),
                    "parent_seller_sku": str(
                        item.get("parent_seller_sku") or ""
                    ).strip(),
                }))
            except ValidationError as exc:
                raise ContractInvalid(
                    f"{tool_name} requires complete business keys"
                ) from exc
        keys = [cls._mode_key(item) for item in requested]
        if len(keys) != len(set(keys)):
            raise ContractInvalid(f"{tool_name} does not accept duplicate business keys")
        return requested

    @staticmethod
    def _mode_key(
        item: CurrentOperatingMode | OperatingModeKey,
    ) -> tuple[str, str, str]:
        return (
            item.shop_id.strip(),
            item.parent_asin.strip().upper(),
            item.parent_seller_sku.strip(),
        )
