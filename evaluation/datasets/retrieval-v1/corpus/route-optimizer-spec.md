# Product specification: Route Optimizer 3

## Goal

Route Optimizer 3 reduces the distance driven per delivery. The target is a 9 percent reduction compared with version 2 on the same delivery set.

## Constraints the solver must respect

Each vehicle has a weight limit and a volume limit. Drivers may not exceed nine hours of driving per day, with a 45 minute break after four and a half hours, as EU rules require. Customers can specify delivery windows as narrow as 30 minutes.

## Rollout

The new solver ships behind the feature flag route_optimizer_v3. It is enabled first for 5 percent of depots, then 25 percent, then all depots, waiting at least one week at each step and rolling back if late deliveries rise by more than 2 percent.
