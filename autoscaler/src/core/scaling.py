#!/usr/bin/env python3
"""
Scaling engine module for making scaling decisions
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from database import DatabaseManager, ScalingRule
from database.mongodb import ScalingEventType

logger = logging.getLogger(__name__)


class ScalingEngine:
    """Makes scaling decisions based on metrics and rules"""

    def __init__(self, config: Dict, database: DatabaseManager):
        """
        Initialize scaling engine

        Args:
            config: Autoscaler configuration
            database: Database manager instance
        """
        self.config = config
        self.database = database
        self.thresholds = config['autoscaler']['thresholds']
        self.limits = config['autoscaler']['limits']

    def evaluate_scaling(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate metrics and determine if scaling is needed

        Args:
            metrics: Current cluster metrics

        Returns:
            Dict containing scaling decision
        """
        decision = {
            "should_scale": False,
            "action": None,
            "count": 0,
            "reason": "",
            "metrics": metrics,
            "timestamp": datetime.utcnow().isoformat()
        }

        try:
            # Check if we're in cooldown
            if self.database.is_cooldown_active("scale_up") and self.database.is_cooldown_active("scale_down"):
                decision["reason"] = "Both scale up and down in cooldown"
                return decision

            current_nodes = metrics.get("current_nodes", 0)
            pending_pods = metrics.get("pending_pods", 0)
            avg_cpu = metrics.get("avg_cpu", 0.0)
            avg_memory = metrics.get("avg_memory", 0.0)

            # Get scaling rules from database
            rules = self.database.get_scaling_rules(enabled_only=True)

            # Evaluate each rule
            for rule in rules:
                # Skip if in cooldown for this action
                if self.database.is_cooldown_active(rule.scale_direction):
                    continue

                # Check if rule conditions are met
                if self._evaluate_rule(rule, metrics, current_nodes):
                    decision["should_scale"] = True
                    # Convert "up"/"down" to "scale_up"/"scale_down"
                    decision["action"] = f"scale_{rule.scale_direction}"
                    decision["count"] = rule.scale_amount
                    decision["reason"] = f"Rule '{rule.name}' triggered: {rule.metric_name} {rule.operator} {rule.threshold}"

                    # Log scaling decision
                    logger.info(f"Scaling decision: {decision['action']} by {decision['count']} nodes - {decision['reason']}")

                    # Record decision in database
                    event_type = ScalingEventType.SCALE_UP if rule.scale_direction == "up" else ScalingEventType.SCALE_DOWN
                    self.database.record_scaling_event(
                        event_type=event_type,
                        old_count=current_nodes,
                        new_count=current_nodes + (rule.scale_amount if rule.scale_direction == "up" else -rule.scale_amount),
                        reason=decision["reason"],
                        metrics=metrics,
                        details={"rule_id": str(rule.id), "rule_name": rule.name}
                    )

                    return decision

            # Fallback to threshold-based logic if no rules matched
            return self._evaluate_thresholds(metrics, current_nodes, pending_pods, avg_cpu, avg_memory)

        except Exception as e:
            logger.error(f"Error evaluating scaling: {e}")
            decision["reason"] = f"Error: {str(e)}"
            return decision

    def _evaluate_rule(self, rule: ScalingRule, metrics: Dict, current_nodes: int) -> bool:
        """Evaluate a single scaling rule"""
        try:
            # Get metric value
            metric_value = self._get_metric_value(rule.metric_name, metrics)
            if metric_value is None:
                logger.warning(f"Unknown metric: {rule.metric_name}")
                return False

            # Evaluate threshold
            if not self._compare_values(metric_value, rule.operator, rule.threshold):
                return False

            # Check additional conditions
            if rule.conditions:
                if "min_nodes" in rule.conditions and current_nodes <= rule.conditions["min_nodes"]:
                    logger.debug(f"Rule '{rule.name}' skipped: at minimum nodes")
                    return False
                if "max_nodes" in rule.conditions and current_nodes >= rule.conditions["max_nodes"]:
                    logger.debug(f"Rule '{rule.name}' skipped: at maximum nodes")
                    return False

            return True

        except Exception as e:
            logger.error(f"Error evaluating rule '{rule.name}': {e}")
            return False

    def _evaluate_thresholds(self, metrics: Dict, current_nodes: int, pending_pods: int,
                           avg_cpu: float, avg_memory: float) -> Dict[str, Any]:
        """Evaluate scaling based on configured thresholds (fallback logic)"""
        decision = {
            "should_scale": False,
            "action": None,
            "count": 0,
            "reason": "",
            "metrics": metrics,
            "timestamp": datetime.utcnow().isoformat()
        }

        # Check scale up conditions
        if not self.database.is_cooldown_active("scale_up"):
            # Check pending pods
            if pending_pods >= self.thresholds["pending_pods"]:
                decision["should_scale"] = True
                decision["action"] = "scale_up"
                decision["count"] = 1
                decision["reason"] = f"{pending_pods} pending pods (threshold: {self.thresholds['pending_pods']})"
                return decision

            # Check CPU threshold
            if avg_cpu >= self.thresholds["cpu_threshold"]:
                decision["should_scale"] = True
                decision["action"] = "scale_up"
                decision["count"] = 1
                decision["reason"] = f"CPU {avg_cpu:.1f}% >= {self.thresholds['cpu_threshold']}%"
                return decision

            # Check memory threshold
            if avg_memory >= self.thresholds["memory_threshold"]:
                decision["should_scale"] = True
                decision["action"] = "scale_up"
                decision["count"] = 1
                decision["reason"] = f"Memory {avg_memory:.1f}% >= {self.thresholds['memory_threshold']}%"
                return decision

        # Check scale down conditions
        if not self.database.is_cooldown_active("scale_down"):
            # Only scale down if above minimum nodes
            if current_nodes > self.limits["min_nodes"]:
                # Check CPU scale down
                if avg_cpu <= self.thresholds["cpu_scale_down"] and avg_memory <= self.thresholds["memory_scale_down"]:
                    decision["should_scale"] = True
                    decision["action"] = "scale_down"
                    decision["count"] = 1
                    decision["reason"] = (f"CPU {avg_cpu:.1f}% <= {self.thresholds['cpu_scale_down']}% "
                                        f"and Memory {avg_memory:.1f}% <= {self.thresholds['memory_scale_down']}%")
                    return decision

        decision["reason"] = "No scaling conditions met"
        return decision

    def _get_metric_value(self, metric_name: str, metrics: Dict) -> Optional[float]:
        """Get value for a specific metric"""
        metric_map = {
            "cpu": metrics.get("avg_cpu", 0),
            "memory": metrics.get("avg_memory", 0),
            "pending_pods": metrics.get("pending_pods", 0),
            "node_count": metrics.get("current_nodes", 0)
        }

        return metric_map.get(metric_name)

    def _compare_values(self, value: float, operator: str, threshold: float) -> bool:
        """Compare two values using the specified operator"""
        try:
            if operator == ">":
                return value > threshold
            elif operator == ">=":
                return value >= threshold
            elif operator == "<":
                return value < threshold
            elif operator == "<=":
                return value <= threshold
            elif operator == "==":
                return value == threshold
            elif operator == "!=":
                return value != threshold
            else:
                logger.warning(f"Unknown operator: {operator}")
                return False
        except Exception as e:
            logger.error(f"Error comparing values: {e}")
            return False

    def predict_scaling_need(self, metrics_history: List[Dict], window_minutes: int = 10) -> Dict[str, Any]:
        """
        Predict scaling need based on historical metrics

        Args:
            metrics_history: List of historical metrics
            window_minutes: Time window to analyze

        Returns:
            Dict containing prediction
        """
        prediction = {
            "will_scale_up": False,
            "will_scale_down": False,
            "confidence": 0.0,
            "reason": "",
            "metrics_trend": {}
        }

        try:
            if len(metrics_history) < 3:
                prediction["reason"] = "Insufficient historical data"
                return prediction

            # Get recent metrics
            recent_metrics = metrics_history[-3:]

            # Calculate trends
            cpu_trend = self._calculate_trend([m.get("avg_cpu", 0) for m in recent_metrics])
            memory_trend = self._calculate_trend([m.get("avg_memory", 0) for m in recent_metrics])
            pending_trend = self._calculate_trend([m.get("pending_pods", 0) for m in recent_metrics])

            prediction["metrics_trend"] = {
                "cpu": cpu_trend,
                "memory": memory_trend,
                "pending_pods": pending_trend
            }

            # Predict based on trends
            current_metrics = metrics_history[-1]

            # Scale up prediction
            if (cpu_trend > 5 or memory_trend > 5 or pending_trend > 0):
                prediction["will_scale_up"] = True
                prediction["confidence"] = min(0.8, max(cpu_trend, memory_trend) / 20)
                prediction["reason"] = f"Increasing trend detected: CPU {cpu_trend:.1f}, Memory {memory_trend:.1f}"

            # Scale down prediction
            elif (cpu_trend < -5 and memory_trend < -5 and
                  current_metrics.get("avg_cpu", 0) < 40 and
                  current_metrics.get("avg_memory", 0) < 40):
                prediction["will_scale_down"] = True
                prediction["confidence"] = min(0.7, abs(cpu_trend) / 20)
                prediction["reason"] = f"Decreasing trend detected with low utilization"

        except Exception as e:
            logger.error(f"Error predicting scaling need: {e}")
            prediction["reason"] = f"Error: {str(e)}"

        return prediction

    def _calculate_trend(self, values: List[float]) -> float:
        """Calculate trend (rate of change) for a list of values"""
        try:
            if len(values) < 2:
                return 0.0

            # Simple linear trend calculation
            n = len(values)
            x = list(range(n))

            # Calculate slope using least squares
            sum_x = sum(x)
            sum_y = sum(values)
            sum_xy = sum(xi * yi for xi, yi in zip(x, values))
            sum_x2 = sum(xi ** 2 for xi in x)

            if n * sum_x2 - sum_x ** 2 == 0:
                return 0.0

            slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x ** 2)

            return slope

        except Exception as e:
            logger.error(f"Error calculating trend: {e}")
            return 0.0