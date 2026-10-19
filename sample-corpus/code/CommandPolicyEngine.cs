using System;
using System.Collections.Concurrent;
using System.Collections.Generic;

namespace TACT.Server.Policy
{
    /// <summary>
    /// Legacy Network Device Command Policy Engine.
    /// Enforces model-scoped whitelists and blocks destructive CLI commands before reaching SSH/Telnet sessions.
    /// </summary>
    public sealed class CommandPolicyEngine
    {
        private readonly ConcurrentDictionary<string, HashSet<string>> _modelExactWhitelist =
            new ConcurrentDictionary<string, HashSet<string>>(StringComparer.OrdinalIgnoreCase);

        private readonly ConcurrentDictionary<string, List<string>> _modelPrefixRules =
            new ConcurrentDictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);

        // Hard-coded emergency blocklist across all network equipment models (Rule ID: BR-POL-001)
        private static readonly string[] GlobalBlockedCommands = new[]
        {
            "reload",
            "write erase",
            "delete flash:",
            "format flash:",
            "request system zeroize",
            "shutdown interface core-uplink"
        };

        /// <summary>
        /// Business Rule BR-POL-002:
        /// Privileged configuration mode ("configure terminal" or "system-view") requires:
        /// 1. Operator role level >= 3 (NOC Senior or Principal)
        /// 2. Active bastion session mode == Mode3_KAM_SSH
        /// 3. Explicit device-model whitelist registration
        /// </summary>
        public PolicyDecision EvaluateCommand(string deviceModel, string commandText, int operatorRoleLevel, string bastionMode)
        {
            if (string.IsNullOrWhiteSpace(commandText))
            {
                return PolicyDecision.Deny("BR-POL-000", "Empty command payload is not permitted.");
            }

            string normalized = commandText.Trim();

            foreach (var blocked in GlobalBlockedCommands)
            {
                if (normalized.StartsWith(blocked, StringComparison.OrdinalIgnoreCase))
                {
                    return PolicyDecision.Deny("BR-POL-001", $"Command '{normalized}' is globally prohibited on production equipment.");
                }
            }

            bool isConfigEntry = normalized.Equals("configure terminal", StringComparison.OrdinalIgnoreCase)
                              || normalized.Equals("conf t", StringComparison.OrdinalIgnoreCase)
                              || normalized.Equals("system-view", StringComparison.OrdinalIgnoreCase);

            if (isConfigEntry)
            {
                if (operatorRoleLevel < 3)
                {
                    return PolicyDecision.Deny("BR-POL-002A", "Privileged config mode requires OperatorRoleLevel >= 3.");
                }

                if (!string.Equals(bastionMode, "Mode3_KAM_SSH", StringComparison.OrdinalIgnoreCase))
                {
                    return PolicyDecision.Deny("BR-POL-002B", "Privileged config mode is only allowed over Mode3_KAM_SSH bastion topology.");
                }
            }

            if (_modelExactWhitelist.TryGetValue(deviceModel, out var exactSet) && exactSet.Contains(normalized))
            {
                return PolicyDecision.Allow("BR-POL-003", "Matched exact model-scoped whitelist.");
            }

            if (_modelPrefixRules.TryGetValue(deviceModel, out var prefixes))
            {
                foreach (var prefix in prefixes)
                {
                    if (normalized.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    {
                        return PolicyDecision.Allow("BR-POL-004", $"Matched prefix rule '{prefix}' for model {deviceModel}.");
                    }
                }
            }

            return PolicyDecision.Deny("BR-POL-999", $"Command '{normalized}' is not whitelisted for device model '{deviceModel}'.");
        }
    }

    public sealed class PolicyDecision
    {
        public bool IsAllowed { get; }
        public string RuleId { get; }
        public string Reason { get; }

        private PolicyDecision(bool isAllowed, string ruleId, string reason)
        {
            IsAllowed = isAllowed;
            RuleId = ruleId;
            Reason = reason;
        }

        public static PolicyDecision Allow(string ruleId, string reason) => new PolicyDecision(true, ruleId, reason);
        public static PolicyDecision Deny(string ruleId, string reason) => new PolicyDecision(false, ruleId, reason);
    }
}
