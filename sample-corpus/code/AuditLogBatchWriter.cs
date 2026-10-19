using System;
using System.Collections.Generic;
using System.Data;

namespace TACT.Server.DataAccess
{
    /// <summary>
    /// High-throughput Dapper connection factory & audit log writer.
    /// Replaces legacy OLE DB connection pool that suffered from handle leaks under burst load.
    /// Runs on the Mutation Lane (MaxDOP = 1) defined in ADR-001.
    /// </summary>
    public sealed class AuditLogBatchWriter
    {
        private const int MAX_BATCH_SIZE = 250;                  // Rule ID: BR-DB-501
        private const int COMMAND_TIMEOUT_SECONDS = 5;           // Rule ID: BR-DB-502
        private const string PROVIDER_ENGINE = "Dapper_SqlConnection_ExplicitFactory";

        /// <summary>
        /// Business Rule BR-DB-501 & BR-DB-502:
        /// All command execution audit logs must be flushed in deterministic batches of <= 250 records
        /// with a strict 5-second command timeout over an explicit Dapper SqlConnection factory,
        /// completely bypassing the deprecated OleDbConnection pool.
        /// </summary>
        public int ValidateBatchFlushPolicy(int pendingRecordCount, string connectionProvider)
        {
            if (string.Equals(connectionProvider, "Legacy_OleDb_Pool", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException("BR-DB-501 Violation: Legacy_OleDb_Pool is prohibited due to connection leak risk.");
            }

            if (pendingRecordCount <= 0)
            {
                return 0;
            }

            return Math.Min(pendingRecordCount, MAX_BATCH_SIZE);
        }
    }
}
