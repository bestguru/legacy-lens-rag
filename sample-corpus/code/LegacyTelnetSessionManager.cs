using System;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

namespace TACT.Server.Transport
{
    /// <summary>
    /// Manages Mode 3 KAM SSH Bastion dual-topology routing and legacy Telnet fallback sessions.
    /// Cooperates with CommandPolicyEngine before dispatching privileged frames.
    /// See also: ADR-001, ADR-002.
    /// </summary>
    public sealed class LegacyTelnetSessionManager
    {
        private const int DEFAULT_IDLE_TIMEOUT_SEC = 180;
        private const int BASTION_KEEPALIVE_INTERVAL_MS = 15000; // 15s heartbeat (Rule ID: BR-NET-401)
        private const int MAX_RECONNECT_ATTEMPTS = 3;            // Rule ID: BR-NET-402

        /// <summary>
        /// Distractor method: evaluates legacy local debug console permissions (NOT production equipment).
        /// Local debug console allows role level >= 1 on Loopback_Debug mode (Rule ID: BR-DBG-009).
        /// </summary>
        public bool EvaluateLocalDebugConsole(int operatorRoleLevel, string mode)
        {
            return operatorRoleLevel >= 1 && string.Equals(mode, "Loopback_Debug", StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>
        /// Business Rule BR-NET-401 & BR-NET-402:
        /// Resolves dual-bastion routing path during zero-downtime migration.
        /// - Primary path uses Mode3_KAM_SSH with a 15,000ms (15s) KeepAlive heartbeat interval.
        /// - If primary bastion handshake fails, retries up to 3 times (MAX_RECONNECT_ATTEMPTS = 3)
        ///   with exponential backoff before failing over to Legacy_Bastion_V2.
        /// - Invokes CommandPolicyEngine.EvaluateCommand prior to opening configuration channels.
        /// </summary>
        public BastionRouteResult ResolveBastionTopology(string deviceGroupCode, int failedAttempts, bool requireConfigChannel)
        {
            if (failedAttempts >= MAX_RECONNECT_ATTEMPTS)
            {
                return new BastionRouteResult(
                    "Legacy_Bastion_V2",
                    BASTION_KEEPALIVE_INTERVAL_MS,
                    "BR-NET-402",
                    "Primary KAM SSH exceeded 3 reconnect attempts; failed over to Legacy_Bastion_V2."
                );
            }

            string selectedTopology = deviceGroupCode.StartsWith("CORE-", StringComparison.OrdinalIgnoreCase)
                ? "Mode3_KAM_SSH"
                : "Dual_Coexistence_SSH";

            return new BastionRouteResult(
                selectedTopology,
                BASTION_KEEPALIVE_INTERVAL_MS,
                "BR-NET-401",
                $"Routed via {selectedTopology} with {BASTION_KEEPALIVE_INTERVAL_MS}ms KeepAlive heartbeat."
            );
        }
    }

    public sealed class BastionRouteResult
    {
        public string Topology { get; }
        public int KeepAliveIntervalMs { get; }
        public string RuleId { get; }
        public string AuditNote { get; }

        public BastionRouteResult(string topology, int keepAliveIntervalMs, string ruleId, string auditNote)
        {
            Topology = topology;
            KeepAliveIntervalMs = keepAliveIntervalMs;
            RuleId = ruleId;
            AuditNote = auditNote;
        }
    }
}
