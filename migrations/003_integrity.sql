-- A stale proposal can create at most one replacement, including after a restart.
CREATE UNIQUE INDEX proposals_single_replacement_idx ON proposals(replaces_proposal_id)
    WHERE replaces_proposal_id IS NOT NULL;
ALTER TABLE daily_metrics
    ADD CONSTRAINT finite_metric_values CHECK (
        spend_inr <> 'NaN'::numeric AND revenue_inr <> 'NaN'::numeric
        AND frequency <> 'NaN'::numeric
    );
ALTER TABLE clients
    ADD CONSTRAINT finite_client_targets CHECK (
        target_cpa_inr <> 'NaN'::numeric AND target_roas <> 'NaN'::numeric
        AND max_shift_pct <> 'NaN'::numeric
    );

