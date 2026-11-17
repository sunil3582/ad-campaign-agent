-- A second migration illustrates upgrades; startup applies only missing files.
CREATE INDEX ad_sets_client_idx ON ad_sets(client_id);
CREATE INDEX metrics_date_idx ON daily_metrics(date);
CREATE INDEX llm_calls_run_idx ON llm_calls(run_id);
CREATE INDEX llm_calls_client_idx ON llm_calls(client_id);
CREATE INDEX proposals_run_idx ON proposals(run_id);
CREATE INDEX proposals_llm_idx ON proposals(llm_call_id);
CREATE INDEX proposals_replaces_idx ON proposals(replaces_proposal_id);
CREATE INDEX proposals_ad_set_idx ON proposals(ad_set_id);
CREATE INDEX proposals_queue_idx ON proposals(next_attempt_at, created_at)
    WHERE status IN ('approved','executing','retry_pending');
CREATE UNIQUE INDEX proposals_one_open_idx ON proposals(client_id, ad_set_id)
    WHERE status IN ('pending_approval','approved','executing','retry_pending');
CREATE INDEX proposals_rejections_idx ON proposals(client_id, reviewed_at DESC)
    WHERE status = 'rejected';
CREATE INDEX audit_proposal_idx ON audit_events(proposal_id);
CREATE INDEX audit_client_idx ON audit_events(client_id);
CREATE INDEX mock_ad_sets_client_idx ON mock_ad_sets(client_id);
CREATE INDEX mock_operations_client_idx ON mock_operations(client_id);

