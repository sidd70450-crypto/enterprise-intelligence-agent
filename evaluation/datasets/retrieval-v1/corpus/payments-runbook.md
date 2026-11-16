# Payments service runbook

## Error E4021: card network timeout

The card network did not answer within eight seconds. Retry the authorisation once after two seconds. If the second attempt also times out, mark the payment as pending and page the payments on-call engineer. Never retry more than once: the network may have approved the first attempt, and a second approval would charge the customer twice.

## Error E5102: duplicate idempotency key

The client reused an idempotency key with a different amount. This is always a client bug. Return the original response for the key and open a ticket with the integration team.

## Settlement

Card payments settle into the operating account two business days after capture. Settlement files arrive from the acquiring bank at 06:00 Central European Time; if the file has not arrived by 07:30, contact the bank's operations desk.
