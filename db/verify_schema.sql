-- 0001_init.sql 의 제약조건이 실제로 동작하는지 확인하는 스모크 테스트.
-- 사용법: psql "$DATABASE_URL" -f db/migrations/0001_init.sql && psql "$DATABASE_URL" -f db/verify_schema.sql
-- ERROR 가 나야 정상인 케이스에는 [MUST FAIL] 을 표기했다.
\set ON_ERROR_STOP off

INSERT INTO policy (policy_id,status,title,category,authority_level,region_codes,
                    benefit_type,benefit_total_krw,apply_start,apply_end,age_min,age_max,schema_json)
VALUES
 ('GG-YONGIN-2026-0042','published','용인시 청년 월세 지원사업','housing','local',
  ARRAY['41465'],'cash_monthly',2400000,'2026-09-01','2026-09-30',19,34,'{}'::jsonb),
 ('MOLIT-2026-YOUTH-RENT','published','청년월세 한시 특별지원','housing','central',
  ARRAY['00'],'cash_monthly',2400000,'2026-03-01','2026-12-31',19,34,'{}'::jsonb),
 ('GG-2026-JOB-0007','published','경기도 청년기본소득','job','province',
  ARRAY['41'],'cash_lump',1000000,'2026-09-01','2026-10-15',24,24,'{}'::jsonb);

\echo '[MUST FAIL] 1. source_quote 공백'
INSERT INTO policy_rule (policy_id,kind,rule_id,field,op,value_json,confidence,source_quote)
VALUES ('GG-YONGIN-2026-0042','eligibility','AGE_RANGE','age','between','[19,34]','CONFIRMED','   ');

\echo '[MUST FAIL] 2. source_quote NULL'
INSERT INTO policy_rule (policy_id,kind,rule_id,field,op,value_json,confidence,source_quote)
VALUES ('GG-YONGIN-2026-0042','eligibility','AGE_RANGE','age','between','[19,34]','CONFIRMED',NULL);

\echo '[MUST PASS] 3. 정상 룰'
INSERT INTO policy_rule (policy_id,kind,rule_id,field,op,value_json,unit,time_satisfiable,confidence,source_quote)
VALUES ('GG-YONGIN-2026-0042','eligibility','AGE_RANGE','age','between','[19,34]','years',false,'CONFIRMED','만 19세 이상 34세 이하 청년'),
       ('GG-YONGIN-2026-0042','eligibility','RESIDENCE_DURATION','residence_months_continuous','>=','6','months',true,'CONFIRMED','신청일 기준 용인시에 6개월 이상 계속하여 거주');

\echo '[MUST FAIL] 4. askable=true 인데 question_template 없음'
INSERT INTO policy_rule (policy_id,kind,rule_id,field,op,value_json,askable,confidence,source_quote)
VALUES ('GG-YONGIN-2026-0042','exclusion','SIMILAR_PROGRAM','similar_program_participation_2y','==','false',true,'ESTIMATED','최근 2년 이내 타 유사사업 참여자는 제외');

\echo '[MUST FAIL] 5. 간선 비정규화 (a > b)'
INSERT INTO policy_conflict_edge (policy_a,policy_b,confidence,source_quote)
VALUES ('MOLIT-2026-YOUTH-RENT','GG-YONGIN-2026-0042','CONFIRMED','국토교통부 청년월세 한시 특별지원과 중복 수혜 불가');

\echo '[MUST PASS] 6. 간선 정규화 (a < b)'
INSERT INTO policy_conflict_edge (policy_a,policy_b,confidence,source_quote)
VALUES ('GG-YONGIN-2026-0042','MOLIT-2026-YOUTH-RENT','CONFIRMED','국토교통부 청년월세 한시 특별지원과 중복 수혜 불가');

\echo '[MUST PASS then FAIL] 7. 활성 스냅샷은 항상 1개'
INSERT INTO snapshot (version,policy_count,rule_count,edge_count,checksum,is_active)
VALUES ('20260914T0200Z-aaa',3,2,1,'sha256:aaa',true);
INSERT INTO snapshot (version,policy_count,rule_count,edge_count,checksum,is_active)
VALUES ('20260915T0200Z-bbb',3,2,1,'sha256:bbb',true);

\echo '[MUST PASS] 8. 지역 접두체인 매칭 — 용인 수지구(41465) → 3건'
SELECT policy_id, region_codes FROM policy
 WHERE status='published' AND region_codes && ARRAY['00','41','41465'] ORDER BY policy_id;

\echo '[MUST PASS] 9. 지역 접두체인 매칭 — 서울 강남구(11680) → 전국 1건'
SELECT policy_id, region_codes FROM policy
 WHERE status='published' AND region_codes && ARRAY['00','11','11680'] ORDER BY policy_id;

\echo '[MUST FAIL] 10. 연령 범위 역전'
INSERT INTO policy (policy_id,status,title,category,authority_level,age_min,age_max,schema_json)
VALUES ('BAD-AGE','draft','x','job','local',40,20,'{}'::jsonb);

\echo '[MUST FAIL] 11. 세션 암호문/논스 짝 위반'
INSERT INTO user_session (profile_ct) VALUES ('\x0102'::bytea);

\echo '[MUST PASS] 12. 세션 정상 — 보관기간 90일 기본값 확인'
INSERT INTO user_session (profile_ct,profile_nonce,birth_year,region_prefix)
VALUES ('\x0102'::bytea,'\x0a0b0c'::bytea,2001,'41')
RETURNING (expires_at::date - created_at::date) AS retention_days;

\echo '[MUST FAIL] 13. 서류 마스터 출처 기록 누락'
INSERT INTO document (doc_code,name,issuer,lead_time_business_days,cost_krw,verified_at)
VALUES ('RESIDENT_REG_ABSTRACT','주민등록초본','정부24',0,0,'2026-09-13');
