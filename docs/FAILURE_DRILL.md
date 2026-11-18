# Failure drill (owner: role 5a)

"Everything fails eventually," so we break the demo stack on purpose and show
it detects the failure, raises an alarm, and recovers without a redeploy. Run
it once before Week 15 and screenshot every step, so the slides have evidence
even if the live demo misbehaves.

Run against `GroupChat-demo` (or your own stack, swapping the owner name) at a
time the team agrees on, never during someone else's load test.

## Before you start

- The stack is deployed from `main` with the alarms (`infra/alarms.py`).
- Optional: deploy once with `-c alarm_email=<you>` and confirm the SNS email
  so alarms arrive in your inbox during the drill.
- Open the dashboard (`DashboardUrl` stack output). The **Alarms** row at the
  top should be all green.
- Open the client (`ClientUrl`) in two browser windows, signed in as two
  users, both in the same room. Send a message to confirm it works.

## Drill 1: the messaging Lambda goes down (Chaos Monkey style)

Setting a Lambda's reserved concurrency to 0 makes AWS refuse every call to
it, which looks exactly like the function being unavailable.

1. **Break it:**

   ```bash
   aws lambda put-function-concurrency --profile msml650 \
     --function-name group-chat-demo-send_message \
     --reserved-concurrent-executions 0
   ```

2. **Show the impact:** send a few messages from the client. They are not
   delivered, and they are not stored, because the handler never ran.
3. **Show detection:** within 1 to 3 minutes (CloudWatch metrics arrive once a
   minute), `send-throttles` and `api-server-errors` turn red on the dashboard
   and, if subscribed, the alarm email arrives. Nobody had to be watching.
4. **Recover:**

   ```bash
   aws lambda delete-function-concurrency --profile msml650 \
     --function-name group-chat-demo-send_message
   ```

5. **Show recovery:** send again; both windows receive it immediately. The
   alarms return to OK within a few minutes and send an OK notification.
   Connections, rooms and history were untouched: nothing had to be
   reconnected, redeployed or restored.

What to say: the managed services (API Gateway, Lambda, DynamoDB) recover from
AWS-side failures on their own across availability zones; this drill simulates
the failure they cannot hide, our own function being unavailable. Detection is
automated; the fix is one command. Messages sent during the outage are lost
because the client does not retry, which is the honest next improvement.

## Drill 2: clients vanish without saying goodbye (criterion 3)

Role 6's check kills clients without a clean close and verifies the system
cleans up after them:

```bash
python tests/smoke/check_stale.py --owner demo --create
```

Then show the dashboard row **3. Stale connections cleaned up**: the bar chart
counts the dead sockets `sendMessage` found and deleted. The `expiresAt` TTL
removes anything both cleanup paths miss.

## Drill 3 (talking point only): restoring chat history

`Messages` has point-in-time recovery on. In the DynamoDB console, Tables,
Messages, Backups, **Restore to point in time** shows any second in the last
35 days can be restored. Screenshot that page; do not run a restore live, it
creates a new table and takes minutes.

## Afterwards

Check reserved concurrency is gone (`aws lambda get-function-concurrency`
should print nothing) and the Alarms row is green before anyone demos.
