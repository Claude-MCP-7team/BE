-- =============================================================================
-- YPC Backend — 0001_init.sql
-- PostgreSQL 15+ / Neon Free 호환 (확장: pgcrypto, pg_trgm)
-- 설계 원칙
--   1) 판정 필터에 쓰는 값만 컬럼으로 승격, 나머지 원본은 JSONB 1개로 보관
--   2) source_quote 는 NOT NULL + 공백금지 → G1 게이트를 DB가 구조적으로 강제
--   3) 개인정보는 애플리케이션 AES-256-GCM 암호화 후 BYTEA 저장 (평문 컬럼 없음)
--   4) 무료 티어 0.5GB 대비: 원문 텍스트는 gzip BYTEA, 이력은 오브젝트 스토리지
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- 정책명 부분검색
-- CREATE EXTENSION IF NOT EXISTS vector;  -- 유사정책 검색(M4 이후 판단). Neon Free 지원 확인됨.

-- 공용: updated_at 자동 갱신
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 1. policy : 정책 마스터
-- -----------------------------------------------------------------------------
CREATE TABLE policy (
  policy_id               TEXT PRIMARY KEY,                       -- 'GG-YONGIN-2026-0042'
  status                  TEXT NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','published','unpublished','expired')),

  -- meta
  title                   TEXT NOT NULL,
  category                TEXT NOT NULL
                            CHECK (category IN ('job','housing','education','welfare','participation')),
  authority_level         TEXT NOT NULL
                            CHECK (authority_level IN ('central','province','local')),
  -- 지역: 접두 체인 매칭용. '00'=전국, '41'=경기, '41465'=용인 수지구
  region_codes            TEXT[] NOT NULL DEFAULT '{}',
  dept_name               TEXT,
  dept_tel                TEXT,

  -- benefit (조합 최적화 가중치)
  benefit_type            TEXT CHECK (benefit_type IN ('cash_lump','cash_monthly','loan','voucher','service')),
  benefit_amount_krw      BIGINT CHECK (benefit_amount_krw >= 0),
  benefit_duration_months SMALLINT CHECK (benefit_duration_months >= 0),
  benefit_total_krw       BIGINT CHECK (benefit_total_krw >= 0),   -- MWIS weight
  benefit_confidence      TEXT NOT NULL DEFAULT 'ESTIMATED'
                            CHECK (benefit_confidence IN ('CONFIRMED','ESTIMATED','NEEDS_REVIEW')),

  -- period
  apply_start             DATE,
  apply_end               DATE,
  is_rolling              BOOLEAN NOT NULL DEFAULT false,
  budget_exhaust_risk     TEXT CHECK (budget_exhaust_risk IN ('low','medium','high')),

  -- 1차 후보 축소용 비정규화 (eligibility 에서 배치가 승격)
  age_min                 SMALLINT,
  age_max                 SMALLINT,

  -- PolicySchema 전문
  schema_json             JSONB NOT NULL,

  -- quality
  parse_confidence        REAL CHECK (parse_confidence BETWEEN 0 AND 1),
  cross_check             TEXT CHECK (cross_check IN ('AGREE','DISAGREE','SKIPPED')),
  needs_review_fields     TEXT[] NOT NULL DEFAULT '{}',

  -- source
  source_api              TEXT,
  source_policy_no        TEXT,
  origin_url              TEXT,
  announcement_url        TEXT,
  content_hash            TEXT,                                   -- sha256:...  증분 갱신 키
  crawled_at              TIMESTAMPTZ,
  last_verified_at        TIMESTAMPTZ,

  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT policy_age_order CHECK (age_min IS NULL OR age_max IS NULL OR age_min <= age_max),
  CONSTRAINT policy_period_order CHECK (apply_start IS NULL OR apply_end IS NULL OR apply_start <= apply_end)
);

CREATE TRIGGER policy_touch BEFORE UPDATE ON policy
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- 스냅샷 빌드/관리자 조회용 인덱스 (실시간 판정은 인메모리라 DB 인덱스를 타지 않음)
CREATE INDEX policy_live_idx      ON policy (apply_end)              WHERE status = 'published';
CREATE INDEX policy_region_gin    ON policy USING GIN (region_codes);
CREATE INDEX policy_age_idx       ON policy (age_min, age_max)       WHERE status = 'published';
CREATE INDEX policy_category_idx  ON policy (category, benefit_type) WHERE status = 'published';
CREATE INDEX policy_hash_idx      ON policy (content_hash);
CREATE INDEX policy_title_trgm    ON policy USING GIN (title gin_trgm_ops);
CREATE INDEX policy_schema_gin    ON policy USING GIN (schema_json jsonb_path_ops);
CREATE INDEX policy_review_idx    ON policy (updated_at)             WHERE needs_review_fields <> '{}';


-- -----------------------------------------------------------------------------
-- 2. policy_rule : eligibility / exclusions 평탄화 (룰 엔진 컴파일 입력)
--    🔴 source_quote NOT NULL = "근거 없는 룰은 저장 불가"를 DB가 강제
-- -----------------------------------------------------------------------------
CREATE TABLE policy_rule (
  id                BIGSERIAL PRIMARY KEY,
  policy_id         TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  kind              TEXT NOT NULL CHECK (kind IN ('eligibility','exclusion')),
  rule_id           TEXT NOT NULL,                                 -- 'AGE_RANGE'
  field             TEXT NOT NULL,                                 -- 'age'
  op                TEXT NOT NULL
                      CHECK (op IN ('==','!=','>','>=','<','<=','between','in','not_in','contains','exists')),
  value_json        JSONB NOT NULL,                                -- 19 | [19,34] | ["41465"]
  unit              TEXT,
  basis             TEXT,                                          -- 'household_incl_parents' 등

  time_satisfiable  BOOLEAN NOT NULL DEFAULT false,                -- 충족 예상일 계산 대상
  ambiguous         BOOLEAN NOT NULL DEFAULT false,                -- LLM 재판정 대상
  askable           BOOLEAN NOT NULL DEFAULT false,                -- 역질문 대상
  question_template TEXT,                                          -- 있으면 C1 LLM 호출 생략

  confidence        TEXT NOT NULL CHECK (confidence IN ('CONFIRMED','ESTIMATED','NEEDS_REVIEW')),
  source_quote      TEXT NOT NULL,
  source_offset     INTEGER,
  source_url        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT policy_rule_quote_not_blank CHECK (length(btrim(source_quote)) > 0),
  CONSTRAINT policy_rule_askable_has_q   CHECK (askable = false OR question_template IS NOT NULL),
  CONSTRAINT policy_rule_uniq            UNIQUE (policy_id, kind, rule_id)
);

CREATE INDEX policy_rule_policy_idx  ON policy_rule (policy_id);
CREATE INDEX policy_rule_field_idx   ON policy_rule (field);
CREATE INDEX policy_rule_askable_idx ON policy_rule (field) WHERE askable;
CREATE INDEX policy_rule_ambig_idx   ON policy_rule (policy_id) WHERE ambiguous;


-- -----------------------------------------------------------------------------
-- 3. policy_conflict : 상충 관계 원본 (공고문에서 추출된 그대로)
-- -----------------------------------------------------------------------------
CREATE TABLE policy_conflict (
  id                  BIGSERIAL PRIMARY KEY,
  policy_id           TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  conflict_type       TEXT NOT NULL
                        CHECK (conflict_type IN ('explicit_policy','category_overlap','same_authority')),
  target_policy_id    TEXT REFERENCES policy(policy_id) ON DELETE CASCADE,  -- explicit_policy 전용
  target_policy_name  TEXT,                                                 -- 미해소 정책명 원문
  target_category     TEXT,
  target_benefit_type TEXT,
  target_authority    TEXT,
  confidence          TEXT NOT NULL CHECK (confidence IN ('CONFIRMED','ESTIMATED')),
  source_quote        TEXT NOT NULL,
  source_url          TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT policy_conflict_quote_not_blank CHECK (length(btrim(source_quote)) > 0),
  CONSTRAINT policy_conflict_target_present CHECK (
    (conflict_type = 'explicit_policy'  AND (target_policy_id IS NOT NULL OR target_policy_name IS NOT NULL))
 OR (conflict_type = 'category_overlap' AND target_category IS NOT NULL)
 OR (conflict_type = 'same_authority'   AND target_authority IS NOT NULL)
  )
);

CREATE INDEX policy_conflict_policy_idx ON policy_conflict (policy_id);
CREATE INDEX policy_conflict_target_idx ON policy_conflict (target_policy_id);


-- -----------------------------------------------------------------------------
-- 4. policy_conflict_edge : 배치가 확정한 무향 간선 (MWIS 솔버가 읽는 유일한 표)
--    항상 a < b 로 정규화 → 중복 간선 불가
-- -----------------------------------------------------------------------------
CREATE TABLE policy_conflict_edge (
  policy_a     TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  policy_b     TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  confidence   TEXT NOT NULL CHECK (confidence IN ('CONFIRMED','ESTIMATED')),
  derived_from BIGINT,                                  -- policy_conflict.id
  source_quote TEXT NOT NULL,
  built_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

  PRIMARY KEY (policy_a, policy_b),
  CONSTRAINT edge_normalized CHECK (policy_a < policy_b)
);

CREATE INDEX edge_b_idx          ON policy_conflict_edge (policy_b);
CREATE INDEX edge_confidence_idx ON policy_conflict_edge (confidence);


-- -----------------------------------------------------------------------------
-- 5. document : 서류 마스터 (30~50종 수작업, BE-M5-1 선행)
-- -----------------------------------------------------------------------------
CREATE TABLE document (
  doc_code                TEXT PRIMARY KEY,                        -- 'RESIDENT_REG_ABSTRACT'
  name                    TEXT NOT NULL,
  aliases                 TEXT[] NOT NULL DEFAULT '{}',            -- 표기 흔들림 흡수 (AI-M5-2)
  issuer                  TEXT NOT NULL,
  issue_channel           TEXT CHECK (issue_channel IN ('online','offline','both')),
  lead_time_business_days SMALLINT NOT NULL DEFAULT 0 CHECK (lead_time_business_days >= 0),
  cost_krw                INTEGER NOT NULL DEFAULT 0 CHECK (cost_krw >= 0),
  notes                   TEXT,
  source_ref              TEXT NOT NULL,                           -- 출처 기록 (분기 검수 근거)
  verified_at             DATE NOT NULL,
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER document_touch BEFORE UPDATE ON document
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE INDEX document_alias_gin ON document USING GIN (aliases);


-- -----------------------------------------------------------------------------
-- 6. policy_document : 정책 ↔ 필요서류
-- -----------------------------------------------------------------------------
CREATE TABLE policy_document (
  policy_id    TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  doc_code     TEXT NOT NULL REFERENCES document(doc_code) ON DELETE RESTRICT,
  required     BOOLEAN NOT NULL DEFAULT true,
  raw_name     TEXT,                                    -- 공고문 원문 표기
  source_quote TEXT,
  PRIMARY KEY (policy_id, doc_code)
);

CREATE INDEX policy_document_doc_idx ON policy_document (doc_code);


-- -----------------------------------------------------------------------------
-- 7. holiday : 영업일 계산 캐시 (한국천문연구원 특일 정보 API)
-- -----------------------------------------------------------------------------
CREATE TABLE holiday (
  d          DATE PRIMARY KEY,
  name       TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'kasi',
  synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- 8. user_session : 익명 세션 (로그인 없음, Q4 확정 전 잠정)
--    🔴 프로필/답변은 앱에서 AES-256-GCM 암호화 후 저장. 평문 컬럼 없음.
-- -----------------------------------------------------------------------------
CREATE TABLE user_session (
  session_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  profile_ct        BYTEA,                               -- 암호문
  profile_nonce     BYTEA,
  answers_ct        BYTEA,
  answers_nonce     BYTEA,
  enc_key_version   SMALLINT NOT NULL DEFAULT 1,         -- 키 로테이션 대비

  -- 식별 불가 수준의 통계 컬럼만 평문 허용
  birth_year        SMALLINT CHECK (birth_year BETWEEN 1900 AND 2100),
  region_prefix     CHAR(2),                             -- 시도 2자리
  employment_status TEXT,

  terms_version     TEXT,
  privacy_agreed_at TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '90 days'),

  CONSTRAINT profile_ct_pair CHECK ((profile_ct IS NULL) = (profile_nonce IS NULL)),
  CONSTRAINT answers_ct_pair CHECK ((answers_ct IS NULL) = (answers_nonce IS NULL))
);

CREATE INDEX user_session_expiry_idx ON user_session (expires_at);   -- 90일 하드 삭제 잡


-- -----------------------------------------------------------------------------
-- 9. judgement_run : 판정 실행 기록 (증분 재판정 · KPI 계측)
-- -----------------------------------------------------------------------------
CREATE TABLE judgement_run (
  run_id           BIGSERIAL PRIMARY KEY,
  session_id       UUID NOT NULL REFERENCES user_session(session_id) ON DELETE CASCADE,
  snapshot_version TEXT NOT NULL,
  profile_hash     TEXT NOT NULL,                        -- 동일 프로필+스냅샷이면 재계산 스킵
  summary          JSONB NOT NULL,                       -- {"eligible":12,"ineligible":130,"needs_info":8}
  result_ct        BYTEA,                                -- 결과 전문(암호화+압축). 없으면 재계산
  latency_ms       INTEGER,
  llm_calls        SMALLINT NOT NULL DEFAULT 0,
  llm_cost_krw     NUMERIC(10,2) NOT NULL DEFAULT 0,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT judgement_run_uniq UNIQUE (session_id, snapshot_version, profile_hash)
);

CREATE INDEX judgement_run_session_idx ON judgement_run (session_id, created_at DESC);


-- -----------------------------------------------------------------------------
-- 10. review_queue : needs_review 관리자 큐 (S8)
-- -----------------------------------------------------------------------------
CREATE TABLE review_queue (
  id             BIGSERIAL PRIMARY KEY,
  policy_id      TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  field_path     TEXT NOT NULL,                          -- 'eligibility[1].value'
  reason         TEXT NOT NULL
                   CHECK (reason IN ('cross_check_disagree','low_confidence','parse_failed',
                                     'missing_source_quote','user_report')),
  agent_value    JSONB,
  verifier_value JSONB,
  status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','in_progress','resolved','wontfix')),
  resolved_value JSONB,
  resolved_by    TEXT,
  resolved_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX review_queue_open_idx   ON review_queue (created_at) WHERE status = 'open';
CREATE INDEX review_queue_policy_idx ON review_queue (policy_id);


-- -----------------------------------------------------------------------------
-- 11. raw_document : 크롤링 원문 (gzip 압축 저장 — 무료 0.5GB 대비)
-- -----------------------------------------------------------------------------
CREATE TABLE raw_document (
  content_hash TEXT PRIMARY KEY,                         -- sha256:...
  policy_id    TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  source_url   TEXT NOT NULL,
  mime         TEXT,
  parser       TEXT CHECK (parser IN ('html','pdfplumber','hwp5','libreoffice','ocr','api_fallback')),
  text_gz      BYTEA,                                    -- gzip(본문)
  char_len     INTEGER,
  extracted_ok BOOLEAN NOT NULL DEFAULT true,
  error_msg    TEXT,
  crawled_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX raw_document_policy_idx ON raw_document (policy_id, crawled_at DESC);


-- -----------------------------------------------------------------------------
-- 12. llm_usage : 토큰/비용 회계 (KPI ≤ 300원/user)
-- -----------------------------------------------------------------------------
CREATE TABLE llm_usage (
  id                  BIGSERIAL PRIMARY KEY,
  agent               TEXT NOT NULL CHECK (agent IN ('A1','A2','VERIFY','C1','C2','RE_JUDGE')),
  model               TEXT NOT NULL,
  policy_id           TEXT,
  session_id          UUID,
  input_tokens        INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cost_krw            NUMERIC(12,4) NOT NULL DEFAULT 0,
  latency_ms          INTEGER,
  ok                  BOOLEAN NOT NULL DEFAULT true,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX llm_usage_time_idx    ON llm_usage (created_at DESC);
CREATE INDEX llm_usage_session_idx ON llm_usage (session_id) WHERE session_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- 13. snapshot : 컴파일된 스냅샷 메타 (활성본은 항상 1개)
-- -----------------------------------------------------------------------------
CREATE TABLE snapshot (
  version      TEXT PRIMARY KEY,                         -- '20260914T0200Z-a1b2c3d'
  policy_count INTEGER NOT NULL,
  rule_count   INTEGER NOT NULL,
  edge_count   INTEGER NOT NULL,
  checksum     TEXT NOT NULL,
  size_bytes   INTEGER,
  artifact_url TEXT,
  is_active    BOOLEAN NOT NULL DEFAULT false,
  built_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX snapshot_active_uniq ON snapshot (is_active) WHERE is_active;


-- -----------------------------------------------------------------------------
-- 14. golden_case : 골든셋 200건 (정확도 하네스, BE-M2-8 / G2 게이트)
-- -----------------------------------------------------------------------------
CREATE TABLE golden_case (
  case_id          TEXT PRIMARY KEY,
  profile_json     JSONB NOT NULL,
  policy_id        TEXT NOT NULL REFERENCES policy(policy_id) ON DELETE CASCADE,
  expected_verdict TEXT NOT NULL CHECK (expected_verdict IN ('ELIGIBLE','INELIGIBLE','NEEDS_INFO')),
  expected_reason  TEXT,
  labeled_by       TEXT NOT NULL,
  labeled_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX golden_case_policy_idx ON golden_case (policy_id);

COMMIT;
