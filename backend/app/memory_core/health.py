"""
Health Check Service (M7)

Health check endpoints for liveness, readiness, and dependency checks.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    """Health status types"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class HealthCheck:
    """Individual health check"""

    def __init__(self, name: str, check_func):
        self.name = name
        self.check_func = check_func
        self.last_check: Optional[datetime] = None
        self.last_status: Optional[HealthStatus] = None
        self.last_error: Optional[str] = None

    async def run(self) -> Dict[str, Any]:
        """Run health check"""
        self.last_check = datetime.utcnow()

        try:
            result = await self.check_func()
            self.last_status = HealthStatus.HEALTHY
            self.last_error = None

            return {
                "name": self.name,
                "status": self.last_status.value,
                "checked_at": self.last_check.isoformat(),
                "details": result,
            }

        except Exception as e:
            self.last_status = HealthStatus.UNHEALTHY
            self.last_error = str(e)

            logger.error(f"Health check {self.name} failed: {e}")

            return {
                "name": self.name,
                "status": self.last_status.value,
                "checked_at": self.last_check.isoformat(),
                "error": self.last_error,
            }


class HealthCheckService:
    """
    Health check service with liveness, readiness, and dependency checks.

    Features:
    - Liveness probe: Is the service running?
    - Readiness probe: Is the service ready to accept requests?
    - Dependency checks: Are all dependencies healthy?
    """

    def __init__(self):
        self.liveness_checks: List[HealthCheck] = []
        self.readiness_checks: List[HealthCheck] = []
        self.dependency_checks: List[HealthCheck] = []

    def register_liveness_check(self, name: str, check_func) -> None:
        """Register liveness check"""
        self.liveness_checks.append(HealthCheck(name, check_func))
        logger.info(f"Registered liveness check: {name}")

    def register_readiness_check(self, name: str, check_func) -> None:
        """Register readiness check"""
        self.readiness_checks.append(HealthCheck(name, check_func))
        logger.info(f"Registered readiness check: {name}")

    def register_dependency_check(self, name: str, check_func) -> None:
        """Register dependency check"""
        self.dependency_checks.append(HealthCheck(name, check_func))
        logger.info(f"Registered dependency check: {name}")

    async def check_liveness(self) -> Dict[str, Any]:
        """
        Check liveness.

        Returns:
            Liveness status
        """
        results = []
        for check in self.liveness_checks:
            result = await check.run()
            results.append(result)

        overall_status = self._calculate_overall_status(results)

        return {
            "status": overall_status,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": results,
        }

    async def check_readiness(self) -> Dict[str, Any]:
        """
        Check readiness.

        Returns:
            Readiness status
        """
        results = []
        for check in self.readiness_checks:
            result = await check.run()
            results.append(result)

        overall_status = self._calculate_overall_status(results)

        return {
            "status": overall_status,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": results,
        }

    async def check_dependencies(self) -> Dict[str, Any]:
        """
        Check dependencies.

        Returns:
            Dependency status
        """
        results = []
        for check in self.dependency_checks:
            result = await check.run()
            results.append(result)

        overall_status = self._calculate_overall_status(results)

        return {
            "status": overall_status,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": results,
        }

    async def check_all(self) -> Dict[str, Any]:
        """
        Check all health aspects.

        Returns:
            Complete health status
        """
        liveness = await self.check_liveness()
        readiness = await self.check_readiness()
        dependencies = await self.check_dependencies()

        # Overall status
        statuses = [liveness["status"], readiness["status"], dependencies["status"]]
        if all(s == HealthStatus.HEALTHY.value for s in statuses):
            overall = HealthStatus.HEALTHY.value
        elif HealthStatus.UNHEALTHY.value in statuses:
            overall = HealthStatus.UNHEALTHY.value
        else:
            overall = HealthStatus.DEGRADED.value

        return {
            "status": overall,
            "timestamp": datetime.utcnow().isoformat(),
            "liveness": liveness,
            "readiness": readiness,
            "dependencies": dependencies,
        }

    def _calculate_overall_status(self, results: List[Dict[str, Any]]) -> str:
        """Calculate overall status from check results"""
        if not results:
            return HealthStatus.HEALTHY.value

        statuses = [r["status"] for r in results]

        if all(s == HealthStatus.HEALTHY.value for s in statuses):
            return HealthStatus.HEALTHY.value
        elif HealthStatus.UNHEALTHY.value in statuses:
            return HealthStatus.UNHEALTHY.value
        else:
            return HealthStatus.DEGRADED.value
