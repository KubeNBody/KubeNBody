# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The Lua scripts of RedisService (redis_service.py): every multi-key step of
the V2 coordination protocol runs as one script, atomically. Each script's
comment gives its KEYS and ARGV and what it returns. The negative return
codes are the constants of redis_service (STALE_GENERATION ... PARTIAL_REJECTED).
"""

# Lua scripts. Each runs atomically on the Redis primary, so the checks and the
# writes they guard cannot interleave with another worker, a recovery or a
# cancel. The meta hash carries the fencing fields:
#   generation       bumped by every recovery. Tasks carry the generation they
#                    were issued under and are rejected once it changes
#   committed_epoch  last epoch whose merged state is in the bodies key. Only
#                    tasks for committed_epoch + 1 may register
# A meta without one of these fields skips the corresponding check. Every job
# has a status, so a meta hash without one (expired, evicted or deleted) is
# treated as a job that is not running: the fence must never let writes
# through because its fields are gone.

_LUA_FENCE = """
local function fence(meta, gen, epoch)
  local m = redis.call('HMGET', meta, 'generation', 'committed_epoch', 'status')
  if not m[3] then return -3 end
  if (m[1] or '0') ~= gen then return -1 end
  if m[2] and tonumber(epoch) ~= tonumber(m[2]) + 1 then return -2 end
  if m[3] == 'CANCELLED' or m[3] == 'FAILED' or m[3] == 'COMPLETED' then return -3 end
  return 0
end
"""

# KEYS: meta, counted, amax, pending_zset, staging, accel_staging, staged, bodies
# ARGV: generation, epoch, package_field, data, amax ('' if none), total_packages,
#       ttl, now, pending_member, accelerations ('' if none), package_num,
#       package_bytes, accel_record_size, body_size
# A package's result is written into its slice of the epoch's staging key, at
# (package_num - 1) * package_bytes, and its returned accelerations into the
# accel staging key, on the package's first registration (`counted` is its
# marker), counting each in the `staged` hash ('s', 'a'). A result that does
# not fit its package (package_bytes, or for the last package exactly the rest
# of the current state), or whose returned rows do not match it, is refused
# with PARTIAL_REJECTED before anything is written, and a state that is gone
# with STATE_MISSING. So the commit's count and length checks prove the
# staging key is the complete new state.
# Returns the number of packages counted, or a negative code.
_REGISTER_PARTIAL = _LUA_FENCE + """
local f = fence(KEYS[1], ARGV[1], ARGV[2])
if f ~= 0 then return f end
local ttl = tonumber(ARGV[7])
local pkg = tonumber(ARGV[11])
local pbytes = tonumber(ARGV[12])
local npkg = tonumber(ARGV[6])
local bsz = tonumber(ARGV[14])
local plen = string.len(ARGV[4])
local n = redis.call('STRLEN', KEYS[8])
if n == 0 then return -6 end
if not pkg or not pbytes or pbytes <= 0 or pkg < 1 or pkg > npkg or plen % bsz ~= 0 then return -9 end
local off = (pkg - 1) * pbytes
if pkg < npkg then
  if plen ~= pbytes then return -9 end
elseif plen == 0 or off + plen ~= n then
  return -9
end
local asz = tonumber(ARGV[13])
if ARGV[10] ~= '' and string.len(ARGV[10]) ~= (plen / bsz) * asz then return -9 end
if ARGV[5] ~= '' then
  redis.call('HSET', KEYS[3], ARGV[3], ARGV[5])
  redis.call('EXPIRE', KEYS[3], ttl)
end
if redis.call('HSETNX', KEYS[2], ARGV[3], '1') == 1 then
  redis.call('SETRANGE', KEYS[5], off, ARGV[4])
  redis.call('HINCRBY', KEYS[7], 's', 1)
  redis.call('EXPIRE', KEYS[5], ttl)
  if ARGV[10] ~= '' then
    redis.call('SETRANGE', KEYS[6], (off / bsz) * asz, ARGV[10])
    redis.call('HINCRBY', KEYS[7], 'a', 1)
    redis.call('EXPIRE', KEYS[6], ttl)
  end
  redis.call('EXPIRE', KEYS[7], ttl)
end
redis.call('EXPIRE', KEYS[2], ttl)
local count = redis.call('HLEN', KEYS[2])
if count >= npkg then
  redis.call('ZADD', KEYS[4], 'NX', ARGV[8], ARGV[9])
end
return count
"""

# KEYS: meta, bodies, accelerations, publish_marker, staging, accel_staging, staged
# ARGV: generation, epoch, job_ttl, next_dt, marker_ttl, sim_time, epoch_dt,
#       total_packages, accel_record_size ('' = do not install a(0) from the
#       accel staging key), body_size
# Commit an epoch: compare-and-set of committed_epoch, exactly one merger
# commits a given epoch. The new state is the staging key the registrations
# filled, renamed into the bodies key (O(1), and replicated as one small
# command). Accepted only when every package staged its slice and the staging
# key is exactly as long as the current state. Otherwise nothing is written
# and STAGING_INCOMPLETE is returned. With returned accelerations complete in
# the accel staging key, they become the next epoch's a(0) in the same step
# (counted as accel_src_returned). Otherwise the previous a(0) is dropped (it
# belongs to the previous state) and the merger ships one.
# Also records the next step in the meta (resume and a takeover whose publish
# marker expired read it there) and writes the publish marker, which holds the
# next step and tells a takeover that publication is still due. epoch_dt is
# kept as committed_dt: a takeover that publishes the epoch after the next one
# was enqueued (which overwrote epoch_dt) records the right step. sim_time is
# the simulation time the new state has reached ('' when unknown, e.g. a job
# recovered from a snapshot older than the time sidecar).
# RENAME keeps the source's TTL: the state's, the meta's and a(0)'s TTL are
# set explicitly, so a job running longer than the TTL keeps its fence. A
# paused job's keys stay without one (see _PAUSE_JOB).
# Returns {1, a(0) installed (1/0), state bytes} committed now,
# {0, 0, state bytes} already committed, or {negative code, 0, 0}.
_COMMIT_STAGED = """
local m = redis.call('HMGET', KEYS[1], 'generation', 'committed_epoch', 'status')
if not m[3] then return {-3, 0, 0} end
if (m[1] or '0') ~= ARGV[1] then return {-1, 0, 0} end
if m[3] == 'CANCELLED' or m[3] == 'FAILED' then return {-3, 0, 0} end
local epoch = tonumber(ARGV[2])
if m[2] then
  local committed = tonumber(m[2])
  if committed >= epoch then return {0, 0, redis.call('STRLEN', KEYS[2])} end
  if committed ~= epoch - 1 then return {-2, 0, 0} end
end
local n = redis.call('STRLEN', KEYS[2])
local p = tonumber(ARGV[8])
local staged = redis.call('HMGET', KEYS[7], 's', 'a')
if n == 0 or tonumber(staged[1] or '0') ~= p or redis.call('STRLEN', KEYS[5]) ~= n then
  return {-8, 0, 0}
end
local paused = (m[3] == 'PAUSED')
local ttl = tonumber(ARGV[3])
redis.call('RENAME', KEYS[5], KEYS[2])
if paused then
  redis.call('PERSIST', KEYS[2])
  redis.call('PERSIST', KEYS[1])
else
  redis.call('EXPIRE', KEYS[2], ttl)
  redis.call('EXPIRE', KEYS[1], ttl)
end
redis.call('DEL', KEYS[3])
local installed = 0
if ARGV[9] ~= '' and tonumber(staged[2] or '0') == p
   and redis.call('STRLEN', KEYS[6]) == (n / tonumber(ARGV[10])) * tonumber(ARGV[9]) then
  redis.call('RENAME', KEYS[6], KEYS[3])
  if paused then redis.call('PERSIST', KEYS[3]) else redis.call('EXPIRE', KEYS[3], ttl) end
  redis.call('HINCRBY', KEYS[1], 'accel_src_returned', 1)
  installed = 1
end
redis.call('HSET', KEYS[1], 'committed_epoch', ARGV[2], 'next_dt', ARGV[4])
if ARGV[6] and ARGV[6] ~= '' then
  redis.call('HSET', KEYS[1], 'sim_time', ARGV[6])
else
  redis.call('HDEL', KEYS[1], 'sim_time')
end
if ARGV[7] and ARGV[7] ~= '' then
  redis.call('HSET', KEYS[1], 'committed_dt', ARGV[7])
end
redis.call('SET', KEYS[4], ARGV[4], 'EX', tonumber(ARGV[5]))
return {1, installed, n}
"""

# KEYS: meta   ARGV: time
_MARK_STARTED = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
return redis.call('HSETNX', KEYS[1], 'started_at', ARGV[1])
"""

# KEYS: meta, then the epoch's coordination keys
# ARGV: generation, epoch
# Delete an epoch's coordination keys only once that epoch is committed in this
# generation. A failover can lose an acknowledged commit. The publisher then
# finds the next epoch refused, and the epoch must stay committable by a
# takeover, so its staged state must survive. Returns the number of keys
# deleted, or a negative fence code (nothing deleted).
_CLEANUP_EPOCH = """
local m = redis.call('HMGET', KEYS[1], 'generation', 'committed_epoch')
if (m[1] or '0') ~= ARGV[1] then return -1 end
if not m[2] or tonumber(m[2]) < tonumber(ARGV[2]) then return -2 end
return redis.call('DEL', unpack(KEYS, 2, #KEYS))
"""

# KEYS: guard, meta, stream
# ARGV: generation, guard_ttl, epoch, epoch_dt, allow_paused ('1'/'0'),
#       n_tasks, n_fields, then n_tasks * n_fields * (field, value)
# Enqueues every task of one epoch exactly once per generation, and only while
# that epoch is the next one to compute (committed_epoch + 1): a late publisher
# or a resume can neither re-issue an old epoch nor roll current_epoch and
# epoch_dt back, even after the guard expired.
_ENQUEUE_EPOCH = """
local m = redis.call('HMGET', KEYS[2], 'generation', 'status', 'committed_epoch')
if not m[2] then return -3 end
if (m[1] or '0') ~= ARGV[1] then return -1 end
if m[2] == 'CANCELLED' or m[2] == 'FAILED' or m[2] == 'COMPLETED' then return -3 end
if m[2] == 'PAUSED' and ARGV[5] ~= '1' then return -4 end
if m[3] and tonumber(ARGV[3]) ~= tonumber(m[3]) + 1 then return -2 end
if not redis.call('SET', KEYS[1], '1', 'NX', 'EX', tonumber(ARGV[2])) then return 0 end
local n_tasks = tonumber(ARGV[6])
local n_fields = tonumber(ARGV[7])
local i = 8
for t = 1, n_tasks do
  local fields = {}
  for k = 1, 2 * n_fields do
    fields[k] = ARGV[i]
    i = i + 1
  end
  redis.call('XADD', KEYS[3], '*', unpack(fields))
end
redis.call('HSET', KEYS[2], 'current_epoch', ARGV[3], 'epoch_dt', ARGV[4])
return n_tasks
"""

# KEYS: meta, accelerations
# ARGV: generation, epoch, data, ttl, source ('returned' / 'fallback', or '')
# Ship a(0) for the state of `epoch` only while that state is the committed
# one of the live generation, and never over a payload already shipped for it
# (the commit drops the previous epoch's payload). Returns 1 written, 0 already
# present, or a negative fence code: a publisher that lost its lease cannot
# attach its accelerations to a later state. No TTL while the job is paused.
_SHIP_ACCELERATIONS = """
local m = redis.call('HMGET', KEYS[1], 'generation', 'committed_epoch', 'status')
if not m[3] then return -3 end
if (m[1] or '0') ~= ARGV[1] then return -1 end
if m[2] and tonumber(m[2]) ~= tonumber(ARGV[2]) then return -2 end
local ok
if m[3] == 'PAUSED' then
  ok = redis.call('SET', KEYS[2], ARGV[3], 'NX')
else
  ok = redis.call('SET', KEYS[2], ARGV[3], 'NX', 'EX', tonumber(ARGV[4]))
end
if ok then
  -- where this a(0) came from (returned / exact / fallback), counted per job
  if ARGV[5] and ARGV[5] ~= '' then redis.call('HINCRBY', KEYS[1], 'accel_src_' .. ARGV[5], 1) end
  return 1
end
return 0
"""

# KEYS: lease, meta
# ARGV: token, lease_ms
# Take the epoch's merge lease (SET NX PX, a replay with the same token, after
# a lost reply, finds the lease its first attempt took and keeps it) and read
# the fence and the previous epoch's simulation time.
# Returns {0} if another worker holds the lease, else
# {1, generation, committed_epoch, status, sim_time} (absent fields nil).
# begin_merge reads the epoch's a_max values with a plain command in the same
# MULTI/EXEC: one round trip, atomic with the fence read.
_BEGIN_MERGE = """
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', tonumber(ARGV[2])) then
  if redis.call('GET', KEYS[1]) ~= ARGV[1] then return {0} end
end
local m = redis.call('HMGET', KEYS[2], 'generation', 'committed_epoch', 'status', 'sim_time')
return {1, m[1], m[2], m[3], m[4]}
"""

# KEYS: meta, the epoch's counted, amax, enqueued, staging, accel_staging,
#       staged (cleanup), the next epoch's enqueued guard, stream, publish
#       marker, pending_zset, merge lease
# ARGV: generation, epoch, token, pending_member, guard_ttl, next_epoch,
#       next_epoch_dt, n_tasks, n_bodies, package_size, job_id, n_tpl, then
#       n_tpl (field, value) pairs (epoch_tasks.epoch_task_template), then the
#       tasks' time_step string
# The publication of a committed, non-final epoch in one round trip: the
# _CLEANUP_EPOCH step, the _ENQUEUE_EPOCH step of the next epoch, the end of
# publication (publish marker and pending entry) and the release of the merge
# lease, in that order and with the same checks and effects as the separate
# scripts. The next epoch's tasks are built here from one job-level template:
# each entry is job_id, epoch, package_num, start_idx, end_idx (package bounds
# as build_epoch_tasks computes them), the template, time_step, generation.
# One case stops early, changing nothing: the commit this publication follows
# was lost (a Redis failover lost it before it reached a replica), seen as the
# live generation's committed_epoch behind the epoch. The epoch then stays
# pending and the lease is left to its holder, so a takeover commits it again.
# Returns {cleanup, enqueue, published}: the _CLEANUP_EPOCH code (keys deleted
# or negative), the _ENQUEUE_EPOCH code (tasks added, 0 already enqueued, or
# negative), and 1 if the publication finished (0: lost commit, nothing done).
_PUBLISH_EPOCH = """
local m = redis.call('HMGET', KEYS[1], 'generation', 'committed_epoch', 'status')
local gen_ok = (m[1] or '0') == ARGV[1]
local epoch = tonumber(ARGV[2])
-- a lost commit: live generation, committed behind the epoch, job not final.
-- Checked first: a paused job's publication would otherwise stop at JOB_PAUSED
-- and end anyway, and resume then finds the epoch's enqueue guard and queues
-- nothing.
if gen_ok and m[2] and tonumber(m[2]) < epoch and m[3] and m[3] ~= 'CANCELLED'
   and m[3] ~= 'FAILED' and m[3] ~= 'COMPLETED' then
  return {-2, -2, 0}
end
-- cleanup (as _CLEANUP_EPOCH)
local cleanup
if not gen_ok then
  cleanup = -1
elseif not m[2] or tonumber(m[2]) < epoch then
  cleanup = -2
else
  cleanup = redis.call('DEL', unpack(KEYS, 2, 7))
end
-- enqueue of the next epoch (as _ENQUEUE_EPOCH, never paused-allowed here)
local enq
if not m[3] then enq = -3
elseif not gen_ok then enq = -1
elseif m[3] == 'CANCELLED' or m[3] == 'FAILED' or m[3] == 'COMPLETED' then enq = -3
elseif m[3] == 'PAUSED' then enq = -4
elseif m[2] and tonumber(ARGV[6]) ~= tonumber(m[2]) + 1 then enq = -2
elseif not redis.call('SET', KEYS[8], '1', 'NX', 'EX', tonumber(ARGV[5])) then enq = 0
else
  local n_tasks = tonumber(ARGV[8])
  local n_bodies = tonumber(ARGV[9])
  local psize = tonumber(ARGV[10])
  local n_tpl = tonumber(ARGV[12])
  for t = 1, n_tasks do
    local e = t * psize
    if e > n_bodies then e = n_bodies end
    local fields = {'job_id', ARGV[11], 'epoch', ARGV[6], 'package_num', tostring(t),
                    'start_idx', tostring((t - 1) * psize), 'end_idx', tostring(e)}
    for k = 1, 2 * n_tpl do fields[#fields + 1] = ARGV[12 + k] end
    fields[#fields + 1] = 'time_step'
    fields[#fields + 1] = ARGV[13 + 2 * n_tpl]
    fields[#fields + 1] = 'generation'
    fields[#fields + 1] = ARGV[1]
    redis.call('XADD', KEYS[9], '*', unpack(fields))
  end
  redis.call('HSET', KEYS[1], 'current_epoch', ARGV[6], 'epoch_dt', ARGV[7])
  enq = n_tasks
end
-- end of publication, then the lease
redis.call('DEL', KEYS[10])
redis.call('ZREM', KEYS[11], ARGV[4])
if redis.call('GET', KEYS[12]) == ARGV[3] then redis.call('DEL', KEYS[12]) end
return {cleanup, enq, 1}
"""

_RENEW_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

_RELEASE_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# A job leaving PAUSED gets the TTL of its meta, state and a(0) back (pause
# removes it). KEYS[1..3] of the scripts below are meta, bodies, accelerations.
_LUA_RESTORE_TTL = """
local function restore_ttl(ttl)
  for i = 1, 3 do
    if redis.call('TTL', KEYS[i]) == -1 then redis.call('EXPIRE', KEYS[i], ttl) end
  end
end
"""

# KEYS: meta, bodies, accelerations
# ARGV: generation ('' = any), reason, ttl, committed_epoch ('' = any)
# Mark a running job FAILED. Refused for a stale generation (a leftover task
# from before a recovery must not fail the recovered job), a committed epoch
# other than the one the caller saw (the watchdog's stalled job made progress
# after the stall was detected), a
# job that already reached a final state, or a meta hash that is gone (never
# recreate it).
_FAIL_JOB = _LUA_RESTORE_TTL + """
local m = redis.call('HMGET', KEYS[1], 'generation', 'status', 'committed_epoch')
if not m[2] then return -3 end
if ARGV[1] ~= '' and (m[1] or '0') ~= ARGV[1] then return -1 end
if ARGV[4] and ARGV[4] ~= '' and (m[3] or '0') ~= ARGV[4] then return -2 end
if m[2] == 'CANCELLED' or m[2] == 'FAILED' or m[2] == 'COMPLETED' then return -3 end
redis.call('HSET', KEYS[1], 'status', 'FAILED', 'error_message', ARGV[2])
restore_ttl(tonumber(ARGV[3]))
return 1
"""

# KEYS: meta, bodies, accelerations
# ARGV: generation, final_epoch, results_file_key, ttl
# Mark a job COMPLETED in one step with the checks, so a cancel or a recovery
# that lands during finalization is not overwritten. Returns 1 completed now,
# 0 already completed, or a negative fence code.
_FINALIZE_JOB = _LUA_RESTORE_TTL + """
local m = redis.call('HMGET', KEYS[1], 'generation', 'committed_epoch', 'status')
if not m[3] then return -3 end
if (m[1] or '0') ~= ARGV[1] then return -1 end
if m[3] == 'COMPLETED' then return 0 end
if m[3] == 'CANCELLED' or m[3] == 'FAILED' then return -3 end
if m[2] and tonumber(m[2]) ~= tonumber(ARGV[2]) then return -2 end
redis.call('HSET', KEYS[1], 'status', 'COMPLETED', 'results_file_key', ARGV[3])
restore_ttl(tonumber(ARGV[4]))
return 1
"""

# KEYS: meta, bodies, accelerations
# Pause a PENDING / IN_PROGRESS job and drop the TTL of its meta, state and
# a(0), so a pause longer than the job TTL loses nothing (resume restores the
# TTL). Returns 1 paused now, 0 already paused, JOB_NOT_RUNNING for a job in a
# final state (never overwritten) or JOB_MISSING.
_PAUSE_JOB = """
local s = redis.call('HGET', KEYS[1], 'status')
if not s then return -5 end
if s ~= 'IN_PROGRESS' and s ~= 'PENDING' and s ~= 'PAUSED' then return -3 end
for i = 1, 3 do redis.call('PERSIST', KEYS[i]) end
if s == 'PAUSED' then return 0 end
redis.call('HSET', KEYS[1], 'status', 'PAUSED')
return 1
"""

# KEYS: meta, bodies, accelerations
# ARGV: ttl
# Resume a PAUSED job that still has an epoch to compute and its state, and
# restore the TTLs. The checks come before the status flip: a job that cannot
# be resumed stays PAUSED instead of turning IN_PROGRESS with nothing queued.
# Returns 1, JOB_NOT_RUNNING (not paused), STALE_EPOCH (no epoch left),
# JOB_MISSING or STATE_MISSING.
_RESUME_JOB = _LUA_RESTORE_TTL + """
local m = redis.call('HMGET', KEYS[1], 'status', 'committed_epoch', 'current_epoch', 'num_epochs')
if not m[1] then return -5 end
if m[1] ~= 'PAUSED' then return -3 end
local done = tonumber(m[2] or m[3] or '0')
if m[4] and done >= tonumber(m[4]) then return -2 end
if redis.call('EXISTS', KEYS[2]) == 0 then return -6 end
redis.call('HSET', KEYS[1], 'status', 'IN_PROGRESS')
restore_ttl(tonumber(ARGV[1]))
return 1
"""

# KEYS: meta, bodies, accelerations
# ARGV: ttl
# Cancel a job that has not reached a final state. Returns 1, JOB_NOT_RUNNING
# (already CANCELLED / FAILED / COMPLETED, left as is) or JOB_MISSING (the
# meta is not recreated).
_CANCEL_JOB = _LUA_RESTORE_TTL + """
local s = redis.call('HGET', KEYS[1], 'status')
if not s then return -5 end
if s == 'CANCELLED' or s == 'FAILED' or s == 'COMPLETED' then return -3 end
redis.call('HSET', KEYS[1], 'status', 'CANCELLED')
restore_ttl(tonumber(ARGV[1]))
return 1
"""

# KEYS: meta, bodies, accelerations, recovery lock
# ARGV: lock token, ttl
# Start a recovery: bump the generation and set the job IN_PROGRESS in one
# step, only while the caller holds the recovery lock and the job is not
# CANCELLED (a cancel always wins over a recovery, FAILED and COMPLETED jobs
# are what recovery is for). A PAUSED job is resumed by its recovery, and gets
# its TTLs back. Returns the new generation, RECOVERY_LOCK_LOST,
# JOB_NOT_RUNNING (cancelled) or JOB_MISSING.
_BEGIN_RECOVERY = _LUA_RESTORE_TTL + """
if redis.call('GET', KEYS[4]) ~= ARGV[1] then return -7 end
local s = redis.call('HGET', KEYS[1], 'status')
if not s then return -5 end
if s == 'CANCELLED' then return -3 end
local gen = redis.call('HINCRBY', KEYS[1], 'generation', 1)
redis.call('HSET', KEYS[1], 'status', 'IN_PROGRESS')
restore_ttl(tonumber(ARGV[2]))
return gen
"""

# KEYS: meta, bodies, accelerations
# ARGV: generation, ttl, state, a(0) ('' = none: workers compute it), epoch
#       restored to, next_dt, sim_time ('' = unknown), committed_dt ('' = keep)
# Write a recovery's state and fencing fields, only while its generation is
# the live one and the job has not been cancelled (or failed) since the
# recovery started. A job paused meanwhile keeps its keys without a TTL (see
# _PAUSE_JOB). Returns 1 or a negative fence code (nothing written).
_RESTORE_STATE = """
local m = redis.call('HMGET', KEYS[1], 'generation', 'status')
if not m[2] then return -3 end
if (m[1] or '0') ~= ARGV[1] then return -1 end
if m[2] == 'CANCELLED' or m[2] == 'FAILED' or m[2] == 'COMPLETED' then return -3 end
redis.call('DEL', KEYS[3])
if m[2] == 'PAUSED' then
  redis.call('SET', KEYS[2], ARGV[3])
  if ARGV[4] ~= '' then redis.call('SET', KEYS[3], ARGV[4]) end
  redis.call('PERSIST', KEYS[1])
else
  local ttl = tonumber(ARGV[2])
  redis.call('SET', KEYS[2], ARGV[3], 'EX', ttl)
  if ARGV[4] ~= '' then redis.call('SET', KEYS[3], ARGV[4], 'EX', ttl) end
  redis.call('EXPIRE', KEYS[1], ttl)
end
redis.call('HSET', KEYS[1], 'current_epoch', ARGV[5], 'committed_epoch', ARGV[5], 'next_dt', ARGV[6])
if ARGV[7] ~= '' then
  redis.call('HSET', KEYS[1], 'sim_time', ARGV[7])
else
  redis.call('HDEL', KEYS[1], 'sim_time')
end
if ARGV[8] ~= '' then redis.call('HSET', KEYS[1], 'committed_dt', ARGV[8]) end
return 1
"""

# KEYS: meta, then the keys to delete
# ARGV: generation
# Delete keys on behalf of a recovery only while its generation is the live
# one: a recovery that was superseded must not delete the keys of the next.
_DELETE_FOR_GENERATION = """
local g = redis.call('HGET', KEYS[1], 'generation')
if (g or '0') ~= ARGV[1] then return -1 end
if #KEYS < 2 then return 0 end
return redis.call('DEL', unpack(KEYS, 2, #KEYS))
"""

# KEYS: stream, retry set
# ARGV: group, consumer, msg_id, delay_ms, deliveries
# A task a worker gave up on (nack) waits out its retry delay outside the
# stream: its entry is acknowledged and its fields parked, with the deliveries
# so far, in a sorted set scored by the due time (server clock). The reclaim
# scan re-adds due ones to the stream as new entries. (Left pending instead,
# the delay could not exceed the stale-task threshold.)
# Returns 1 parked. 0 not pending for this consumer (reclaimed by another, or acked).
_PARK_TASK = """
local p = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1, ARGV[2])
if #p == 0 then return 0 end
local e = redis.call('XRANGE', KEYS[1], ARGV[3], ARGV[3])
redis.call('XACK', KEYS[1], ARGV[1], ARGV[3])
if #e == 0 then return 0 end
local f = {}
local src = e[1][2]
for i = 1, #src, 2 do
  if src[i] ~= '__prior_deliveries__' then
    f[#f + 1] = src[i]
    f[#f + 1] = src[i + 1]
  end
end
f[#f + 1] = '__prior_deliveries__'
f[#f + 1] = ARGV[5]
local t = redis.call('TIME')
local due = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000) + tonumber(ARGV[4])
redis.call('ZADD', KEYS[2], due, cjson.encode({id = ARGV[3], f = f}))
return 1
"""

# Re-add the parked tasks that are due (at most ARGV[1]) to the stream.
# KEYS: stream, retry set. Returns how many.
_PROMOTE_DUE_TASKS = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local due = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now, 'LIMIT', 0, tonumber(ARGV[1]))
for _, m in ipairs(due) do
  redis.call('ZREM', KEYS[2], m)
  local d = cjson.decode(m)
  redis.call('XADD', KEYS[1], '*', unpack(d.f))
end
return #due
"""

# KEYS: stream
# ARGV: group, consumer, message id
# Heartbeat of a task being processed: reset its pending idle time (XCLAIM to
# the same consumer, JUSTID so the delivery count is not incremented), but only
# while this consumer still owns it, so a heartbeat never steals back a task
# that was reclaimed meanwhile. Returns 1 if refreshed, 0 if no longer owned.
_TOUCH_TASK = """
local p = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1, ARGV[2])
if #p == 0 then return 0 end
redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0, ARGV[3], 'JUSTID')
return 1
"""
