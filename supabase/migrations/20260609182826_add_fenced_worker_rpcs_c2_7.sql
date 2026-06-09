-- ═══════════════════════════════════════════════════════════════════
-- C2.7 commit 5/8: fenced worker queue RPC functions
-- ═══════════════════════════════════════════════════════════════════
-- Migration version: 20260609182826
-- Applied via Supabase MCP apply_migration on 2026-06-09.
--
-- 8 個 SECURITY DEFINER functions, 全部 SET search_path=public.
-- 全部 fenced UPDATE 包含: claim_token + worker_id + worker_state +
-- lease_expires_at > NOW(), 不採 check-then-update.
-- service_role 限定執行; anon / authenticated / PUBLIC 一律 REVOKE.
--
-- Verification (post-apply, 41/41 PASS):
--   T1-T9   claim / heartbeat / reclaim / token 不同 / attempt_count
--   T10-12  fenced update_progress + merge_result (含 race protection)
--   T13-14  finalize completed + terminal 鎖
--   T15     finalize failed
--   T16-17  max_attempts + fail_exhausted
--   T18-19  active lease 不被誤殺
--   T20-22  watchdog (completed/failed 不碰; legacy stale; anchored starvation)
--   T23a-g  input validation (NULL worker_id / wrong mode / 短/長 lease /
--           progress 過大 / 寫 terminal status / finalize non-terminal)
--   T24     anon / auth / PUBLIC 已 REVOKE; service_role only
--   T25     fixture 全清, row count 回到 37
--   RACE    舊 worker 用舊 token merge/finalize 都 = false; 新 worker 不被覆寫
--
-- 本 commit 不含:
--   - worker.py / db_queue.py (commit 6)
--   - api.py routing 切換 (commit 7)
--   - Railway worker service / pg_cron schedule (commit 8 非 git)

-- ─── 1. worker_claim_job ───────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.worker_claim_job(
  p_worker_id      text,
  p_execution_mode text,
  p_lease_seconds  integer DEFAULT 900
)
RETURNS TABLE (
  job_id            text,
  claim_token       uuid,
  attempt_count     integer,
  queue_payload     jsonb,
  prev_claimed_by   text,
  prev_worker_state text,
  is_reclaim        boolean
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_token uuid := gen_random_uuid();
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
    RAISE EXCEPTION 'worker_id must not be null or empty';
  END IF;
  IF p_execution_mode NOT IN ('legacy','anchored') THEN
    RAISE EXCEPTION 'invalid execution_mode: %', p_execution_mode;
  END IF;
  IF p_lease_seconds IS NULL OR p_lease_seconds < 60 OR p_lease_seconds > 1800 THEN
    RAISE EXCEPTION 'lease_seconds must be 60..1800, got %', p_lease_seconds;
  END IF;

  RETURN QUERY
  WITH next_job AS (
    SELECT o.job_id, o.worker_state, o.claimed_by, o.attempt_count, o.max_attempts
    FROM public.orders o
    WHERE o.execution_mode = p_execution_mode
      AND (
        (o.worker_state = 'unclaimed')
        OR
        (o.worker_state IN ('claimed','running','finalizing')
         AND o.lease_expires_at IS NOT NULL
         AND o.lease_expires_at < NOW()
         AND o.attempt_count < o.max_attempts)
      )
    ORDER BY o.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
  )
  UPDATE public.orders o
  SET
    worker_state     = 'claimed',
    claim_token      = v_token,
    claimed_by       = p_worker_id,
    claimed_at       = NOW(),
    lease_expires_at = NOW() + (p_lease_seconds || ' seconds')::interval,
    heartbeat_at     = NOW(),
    attempt_count    = nj.attempt_count + 1,
    updated_at       = NOW(),
    last_error_type  = CASE
      WHEN nj.worker_state IN ('claimed','running','finalizing')
        THEN 'WorkerLeaseExpired'
      ELSE o.last_error_type
    END,
    result_json      = CASE
      WHEN nj.worker_state IN ('claimed','running','finalizing') THEN
        COALESCE(o.result_json, '{}'::jsonb) || jsonb_build_object(
          'last_error_type',       'WorkerLeaseExpired',
          'previous_claimed_by',   nj.claimed_by,
          'previous_worker_state', nj.worker_state,
          'reclaim_at',            to_jsonb(NOW()),
          'reclaim_attempt',       nj.attempt_count + 1
        )
      ELSE o.result_json
    END
  FROM next_job nj
  WHERE o.job_id = nj.job_id
  RETURNING
    o.job_id,
    o.claim_token,
    o.attempt_count,
    o.queue_payload,
    nj.claimed_by                       AS prev_claimed_by,
    nj.worker_state                     AS prev_worker_state,
    (nj.worker_state <> 'unclaimed')    AS is_reclaim;
END;
$$;

-- ─── 2. worker_heartbeat ───────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.worker_heartbeat(
  p_worker_id     text,
  p_job_id        text,
  p_claim_token   uuid,
  p_lease_seconds integer DEFAULT 900
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_updated integer;
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
    RAISE EXCEPTION 'worker_id must not be null or empty';
  END IF;
  IF p_claim_token IS NULL THEN
    RAISE EXCEPTION 'claim_token must not be null';
  END IF;
  IF p_lease_seconds IS NULL OR p_lease_seconds < 60 OR p_lease_seconds > 1800 THEN
    RAISE EXCEPTION 'lease_seconds must be 60..1800, got %', p_lease_seconds;
  END IF;

  UPDATE public.orders
  SET
    heartbeat_at     = NOW(),
    lease_expires_at = NOW() + (p_lease_seconds || ' seconds')::interval,
    updated_at       = NOW(),
    worker_state     = CASE WHEN worker_state = 'claimed'
                            THEN 'running' ELSE worker_state END
  WHERE job_id           = p_job_id
    AND claimed_by       = p_worker_id
    AND claim_token      = p_claim_token
    AND worker_state     IN ('claimed','running','finalizing')
    AND lease_expires_at > NOW();

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated > 0;
END;
$$;

-- ─── 3. worker_update_progress ─────────────────────────────────────
CREATE OR REPLACE FUNCTION public.worker_update_progress(
  p_worker_id   text,
  p_job_id      text,
  p_claim_token uuid,
  p_status      text,
  p_progress    integer,
  p_message     text
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_updated integer;
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
    RAISE EXCEPTION 'worker_id must not be null or empty';
  END IF;
  IF p_claim_token IS NULL THEN
    RAISE EXCEPTION 'claim_token must not be null';
  END IF;
  IF p_status IS NULL THEN
    RAISE EXCEPTION 'status must not be null';
  END IF;
  IF p_status IN ('completed','failed') THEN
    RAISE EXCEPTION 'use worker_finalize for terminal status: %', p_status;
  END IF;
  IF p_progress IS NULL OR p_progress < 0 OR p_progress > 99 THEN
    RAISE EXCEPTION 'progress must be 0..99, got %', p_progress;
  END IF;

  UPDATE public.orders
  SET
    status     = p_status,
    progress   = p_progress,
    message    = p_message,
    updated_at = NOW()
  WHERE job_id           = p_job_id
    AND claimed_by       = p_worker_id
    AND claim_token      = p_claim_token
    AND worker_state     IN ('claimed','running','finalizing')
    AND lease_expires_at > NOW();

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated > 0;
END;
$$;

-- ─── 4. worker_merge_result ────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.worker_merge_result(
  p_worker_id         text,
  p_job_id            text,
  p_claim_token       uuid,
  p_result_json_merge jsonb
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_updated integer;
  v_merge   jsonb;
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
    RAISE EXCEPTION 'worker_id must not be null or empty';
  END IF;
  IF p_claim_token IS NULL THEN
    RAISE EXCEPTION 'claim_token must not be null';
  END IF;
  v_merge := COALESCE(p_result_json_merge, '{}'::jsonb);

  UPDATE public.orders
  SET
    result_json = COALESCE(result_json, '{}'::jsonb) || v_merge,
    updated_at  = NOW()
  WHERE job_id           = p_job_id
    AND claimed_by       = p_worker_id
    AND claim_token      = p_claim_token
    AND worker_state     IN ('claimed','running','finalizing')
    AND lease_expires_at > NOW();

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated > 0;
END;
$$;

-- ─── 5. worker_set_state ───────────────────────────────────────────
-- 只允許 running / finalizing; released 由 worker_finalize 寫入.
CREATE OR REPLACE FUNCTION public.worker_set_state(
  p_worker_id   text,
  p_job_id      text,
  p_claim_token uuid,
  p_new_state   text
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_updated integer;
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
    RAISE EXCEPTION 'worker_id must not be null or empty';
  END IF;
  IF p_claim_token IS NULL THEN
    RAISE EXCEPTION 'claim_token must not be null';
  END IF;
  IF p_new_state IS NULL OR p_new_state NOT IN ('running','finalizing') THEN
    RAISE EXCEPTION 'set_state only allows running/finalizing; use worker_finalize for released. got: %', p_new_state;
  END IF;

  UPDATE public.orders
  SET
    worker_state = p_new_state,
    updated_at   = NOW()
  WHERE job_id           = p_job_id
    AND claimed_by       = p_worker_id
    AND claim_token      = p_claim_token
    AND worker_state     IN ('claimed','running','finalizing')
    AND lease_expires_at > NOW();

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated > 0;
END;
$$;

-- ─── 6. worker_finalize ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.worker_finalize(
  p_worker_id         text,
  p_job_id            text,
  p_claim_token       uuid,
  p_final_status      text,
  p_final_message     text,
  p_result_json_merge jsonb
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_updated integer;
  v_merge   jsonb;
BEGIN
  IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
    RAISE EXCEPTION 'worker_id must not be null or empty';
  END IF;
  IF p_claim_token IS NULL THEN
    RAISE EXCEPTION 'claim_token must not be null';
  END IF;
  IF p_final_status IS NULL OR p_final_status NOT IN ('completed','failed') THEN
    RAISE EXCEPTION 'final_status must be completed/failed, got %', p_final_status;
  END IF;
  v_merge := COALESCE(p_result_json_merge, '{}'::jsonb);

  UPDATE public.orders
  SET
    status       = p_final_status,
    progress     = CASE WHEN p_final_status = 'completed' THEN 100 ELSE 0 END,
    message      = p_final_message,
    worker_state = 'released',
    result_json  = COALESCE(result_json, '{}'::jsonb) || v_merge,
    updated_at   = NOW()
  WHERE job_id           = p_job_id
    AND claimed_by       = p_worker_id
    AND claim_token      = p_claim_token
    AND worker_state     IN ('claimed','running','finalizing')
    AND lease_expires_at > NOW();

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated > 0;
END;
$$;

-- ─── 7. worker_fail_exhausted_attempts ─────────────────────────────
CREATE OR REPLACE FUNCTION public.worker_fail_exhausted_attempts()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_count integer;
BEGIN
  WITH exhausted AS (
    SELECT o.job_id, o.result_json AS prev_result_json,
           o.claimed_by, o.attempt_count,
           o.status AS prev_status, o.progress AS prev_progress
    FROM public.orders o
    WHERE o.worker_state IN ('claimed','running','finalizing')
      AND o.lease_expires_at IS NOT NULL
      AND o.lease_expires_at < NOW()
      AND o.attempt_count >= o.max_attempts
    FOR UPDATE SKIP LOCKED
  )
  UPDATE public.orders o
  SET
    status          = 'failed',
    progress        = 0,
    message         = '系統重試多次仍未成功，請聯絡客服或重新下單',
    worker_state    = 'released',
    last_error_type = 'MaxAttemptsExceeded',
    updated_at      = NOW(),
    result_json     = COALESCE(e.prev_result_json, '{}'::jsonb) || jsonb_build_object(
      'error',           'attempt count exhausted after worker death(s)',
      'error_type',      'MaxAttemptsExceeded',
      'failed_stage',    e.prev_status,
      'last_progress',   e.prev_progress,
      'last_attempt_by', e.claimed_by,
      'attempt_count',   e.attempt_count,
      'failed_at',       to_jsonb(NOW())
    )
  FROM exhausted e
  WHERE o.job_id = e.job_id;

  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END;
$$;

-- ─── 8. watchdog_mark_stale_jobs ───────────────────────────────────
-- 本 commit 只建 function. pg_cron schedule 留到 commit 8.
CREATE OR REPLACE FUNCTION public.watchdog_mark_stale_jobs()
RETURNS TABLE (
  job_id          text,
  prev_status     text,
  prev_progress   integer,
  prev_updated_at timestamptz,
  reason          text
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  RETURN QUERY
  WITH stale AS (
    SELECT
      o.job_id,
      o.status                          AS prev_status,
      o.progress                        AS prev_progress,
      o.updated_at                      AS prev_updated_at,
      o.result_json                     AS prev_result_json,
      o.worker_state,
      CASE
        WHEN o.worker_state IN ('claimed','running','finalizing')
             AND o.lease_expires_at < NOW()
             AND o.attempt_count >= o.max_attempts
          THEN 'processing_lease_expired_max_attempts'
        WHEN o.execution_mode = 'anchored' AND o.worker_state = 'unclaimed'
             AND o.updated_at < NOW() - INTERVAL '5 minutes'
          THEN 'anchored_queue_starvation'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'queued'
             AND o.updated_at < NOW() - INTERVAL '2 minutes'
          THEN 'legacy_queued_timeout'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'downloading'
             AND o.updated_at < NOW() - INTERVAL '2 minutes'
          THEN 'legacy_downloading_timeout'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'analyzing'
             AND o.updated_at < NOW() - INTERVAL '6 minutes'
          THEN 'legacy_analyzing_timeout'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'zoning'
             AND o.updated_at < NOW() - INTERVAL '3 minutes'
          THEN 'legacy_zoning_timeout'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'matching'
             AND o.updated_at < NOW() - INTERVAL '2 minutes'
          THEN 'legacy_matching_timeout'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'rendering'
             AND o.updated_at < NOW() - INTERVAL '10 minutes'
          THEN 'legacy_rendering_timeout'
        WHEN o.execution_mode = 'legacy' AND o.worker_state IS NULL
             AND o.status = 'validating'
             AND o.updated_at < NOW() - INTERVAL '4 minutes'
          THEN 'legacy_validating_timeout'
        ELSE NULL
      END AS reason
    FROM public.orders o
    WHERE o.status NOT IN ('completed', 'failed')
      AND (
        (o.worker_state IN ('claimed','running','finalizing')
         AND o.lease_expires_at < NOW()
         AND o.attempt_count >= o.max_attempts)
        OR
        (o.execution_mode = 'anchored' AND o.worker_state = 'unclaimed')
        OR
        (o.execution_mode = 'legacy' AND o.worker_state IS NULL
         AND o.status IN ('queued','downloading','analyzing','zoning',
                          'matching','rendering','validating'))
      )
    FOR UPDATE SKIP LOCKED
  )
  UPDATE public.orders o
  SET
    status          = 'failed',
    progress        = 0,
    message         = '系統長時間無回應，請重新嘗試或聯絡客服',
    worker_state    = CASE WHEN s.worker_state IS NOT NULL
                           THEN 'released' ELSE o.worker_state END,
    last_error_type = 'StaleJobTimeout',
    updated_at      = NOW(),
    result_json     = COALESCE(s.prev_result_json, '{}'::jsonb) || jsonb_build_object(
      'error',           'stale job: ' || s.reason,
      'error_type',      'StaleJobTimeout',
      'failed_stage',    s.prev_status,
      'last_progress',   s.prev_progress,
      'failed_at',       to_jsonb(NOW()),
      'watchdog_reason', s.reason,
      'watchdog_note',   'marked by watchdog_mark_stale_jobs; no automatic regeneration'
    )
  FROM stale s
  WHERE o.job_id = s.job_id
    AND s.reason IS NOT NULL
  RETURNING
    o.job_id,
    s.prev_status,
    s.prev_progress,
    s.prev_updated_at,
    s.reason;
END;
$$;

-- ═══════════════════════════════════════════════════════════════════
-- 權限: 全部 service_role 限定; anon / authenticated / PUBLIC 一律 REVOKE.
-- 使用完整 function signature 避免 overload 撤權失敗.
-- ═══════════════════════════════════════════════════════════════════

REVOKE EXECUTE ON FUNCTION public.worker_claim_job(text, text, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.worker_heartbeat(text, text, uuid, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.worker_update_progress(text, text, uuid, text, integer, text)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.worker_merge_result(text, text, uuid, jsonb)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.worker_set_state(text, text, uuid, text)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.worker_finalize(text, text, uuid, text, text, jsonb)
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.worker_fail_exhausted_attempts()
  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.watchdog_mark_stale_jobs()
  FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.worker_claim_job(text, text, integer)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.worker_heartbeat(text, text, uuid, integer)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.worker_update_progress(text, text, uuid, text, integer, text)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.worker_merge_result(text, text, uuid, jsonb)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.worker_set_state(text, text, uuid, text)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.worker_finalize(text, text, uuid, text, text, jsonb)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.worker_fail_exhausted_attempts()
  TO service_role;
GRANT EXECUTE ON FUNCTION public.watchdog_mark_stale_jobs()
  TO service_role;
