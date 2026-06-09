-- ═══════════════════════════════════════════════════════════════════
-- C2.7 commit 4/8: worker queue schema (idempotent)
-- ═══════════════════════════════════════════════════════════════════
-- Migration version: 20260609181928
-- Applied via Supabase MCP apply_migration on 2026-06-09.
--
-- 行為承諾:
--   - 既有 37 個 orders 全部 execution_mode='legacy', worker_state=NULL,
--     attempt_count=0, max_attempts=2 → 0 行為變化.
--   - 本 commit 不建 RPC, 不開 pg_cron, 不改 production code.
--   - pgcrypto 已存在 (gen_random_uuid 可用), 不重複建.
--
-- Verification (post-apply):
--   - 11 new columns 全部存在且型別正確
--   - 既有 37 row, post-fixture-cleanup 仍 37 (0 row 受影響)
--   - 4 constraints + 1 partial index 全部部署
--   - 3 illegal value 測試全部被擋
--     (execution_mode='wrong', worker_state='wrong', attempt_count=-1)
--   - 1 legal fixture row 寫入成功 (anchored / unclaimed / auto uuid /
--     jsonb queue_payload), 已清除
--   - /health 200 (production code 未動)

-- ─── 11 個欄位 ─────────────────────────────────────────────────────
ALTER TABLE public.orders
  ADD COLUMN IF NOT EXISTS execution_mode    text       NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS worker_state      text,
  ADD COLUMN IF NOT EXISTS claim_token       uuid,
  ADD COLUMN IF NOT EXISTS claimed_by        text,
  ADD COLUMN IF NOT EXISTS claimed_at        timestamptz,
  ADD COLUMN IF NOT EXISTS lease_expires_at  timestamptz,
  ADD COLUMN IF NOT EXISTS heartbeat_at      timestamptz,
  ADD COLUMN IF NOT EXISTS attempt_count     integer    NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_attempts      integer    NOT NULL DEFAULT 2,
  ADD COLUMN IF NOT EXISTS queue_payload     jsonb,
  ADD COLUMN IF NOT EXISTS last_error_type   text;

-- ─── Constraints (idempotent via DO + pg_constraint 守門) ──────────
DO $$
BEGIN
  -- execution_mode whitelist (合法值: legacy / anchored)
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    WHERE n.nspname='public' AND t.relname='orders'
      AND c.conname='orders_execution_mode_chk'
  ) THEN
    ALTER TABLE public.orders
      ADD CONSTRAINT orders_execution_mode_chk
      CHECK (execution_mode IN ('legacy','anchored'));
  END IF;

  -- worker_state whitelist (NULL 允許; 其餘只能是 5 種狀態)
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    WHERE n.nspname='public' AND t.relname='orders'
      AND c.conname='orders_worker_state_chk'
  ) THEN
    ALTER TABLE public.orders
      ADD CONSTRAINT orders_worker_state_chk
      CHECK (worker_state IS NULL OR worker_state IN
        ('unclaimed','claimed','running','finalizing','released'));
  END IF;

  -- attempt_count >= 0
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    WHERE n.nspname='public' AND t.relname='orders'
      AND c.conname='orders_attempt_count_chk'
  ) THEN
    ALTER TABLE public.orders
      ADD CONSTRAINT orders_attempt_count_chk
      CHECK (attempt_count >= 0);
  END IF;

  -- max_attempts >= 1
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    JOIN pg_class t ON t.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=t.relnamespace
    WHERE n.nspname='public' AND t.relname='orders'
      AND c.conname='orders_max_attempts_chk'
  ) THEN
    ALTER TABLE public.orders
      ADD CONSTRAINT orders_max_attempts_chk
      CHECK (max_attempts >= 1);
  END IF;
END $$;

-- ─── Partial index (worker claim hot path) ─────────────────────────
CREATE INDEX IF NOT EXISTS orders_worker_claim_idx
ON public.orders (
  execution_mode,
  worker_state,
  lease_expires_at,
  created_at
)
WHERE worker_state IN ('unclaimed','claimed','running','finalizing');
