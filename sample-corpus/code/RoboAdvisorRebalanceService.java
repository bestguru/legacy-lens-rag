package com.enterprise.wealth.robo;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Map;

/**
 * Legacy Robo-Advisor Portfolio Rebalancing & Risk Engine.
 * Extracted during Next-Gen Financial Risk Management Modernization.
 */
public class RoboAdvisorRebalanceService {

    private static final BigDecimal MAX_EQUITY_CAP_CONSERVATIVE = new BigDecimal("0.20");
    private static final BigDecimal MAX_EQUITY_CAP_BALANCED = new BigDecimal("0.50");
    private static final BigDecimal DRIFT_THRESHOLD_TRIGGER = new BigDecimal("0.035"); // 3.5% drift threshold (Rule ID: BR-FIN-101)
    private static final BigDecimal HIGH_VOLATILITY_VIX_CUTOFF = new BigDecimal("28.5"); // Rule ID: BR-FIN-102

    /**
     * Business Rule BR-FIN-101 & BR-FIN-102:
     * Determines whether an automated discretionary pension portfolio requires rebalancing.
     * - Rebalancing triggers only if absolute asset-class drift >= 3.5% (0.035).
     * - If market volatility index (VIX) exceeds 28.5, equity buy orders are halted and shifted to sovereign bonds.
     */
    public RebalanceDecision evaluatePortfolioDrift(
            String riskProfile,
            BigDecimal currentEquityWeight,
            BigDecimal targetEquityWeight,
            BigDecimal currentVixIndex) {

        BigDecimal maxAllowedEquity = "CONSERVATIVE".equalsIgnoreCase(riskProfile)
                ? MAX_EQUITY_CAP_CONSERVATIVE
                : MAX_EQUITY_CAP_BALANCED;

        if (currentEquityWeight.compareTo(maxAllowedEquity) > 0) {
            return new RebalanceDecision(
                    true,
                    "BR-FIN-100",
                    "FORCE_DELEVERAGE_EQUITY",
                    currentEquityWeight.subtract(maxAllowedEquity).setScale(4, RoundingMode.HALF_UP)
            );
        }

        BigDecimal absoluteDrift = currentEquityWeight.subtract(targetEquityWeight).abs();
        if (absoluteDrift.compareTo(DRIFT_THRESHOLD_TRIGGER) < 0) {
            return new RebalanceDecision(false, "BR-FIN-101", "HOLD_WITHIN_DRIFT_BAND", absoluteDrift);
        }

        if (currentVixIndex.compareTo(HIGH_VOLATILITY_VIX_CUTOFF) > 0
                && currentEquityWeight.compareTo(targetEquityWeight) < 0) {
            return new RebalanceDecision(
                    false,
                    "BR-FIN-102",
                    "HALT_EQUITY_BUY_VIX_CIRCUIT_BREAKER",
                    absoluteDrift
            );
        }

        return new RebalanceDecision(true, "BR-FIN-103", "EXECUTE_STANDARD_REBALANCE", absoluteDrift);
    }

    public static final class RebalanceDecision {
        private final boolean rebalanceRequired;
        private final String ruleId;
        private final String actionCode;
        private final BigDecimal metricValue;

        public RebalanceDecision(boolean rebalanceRequired, String ruleId, String actionCode, BigDecimal metricValue) {
            this.rebalanceRequired = rebalanceRequired;
            this.ruleId = ruleId;
            this.actionCode = actionCode;
            this.metricValue = metricValue;
        }

        public boolean isRebalanceRequired() { return rebalanceRequired; }
        public String getRuleId() { return ruleId; }
        public String getActionCode() { return actionCode; }
        public BigDecimal getMetricValue() { return metricValue; }
    }
}
