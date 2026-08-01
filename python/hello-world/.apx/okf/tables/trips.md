---
type: Unity Catalog Table
title: trips
description: NYC yellow taxi trips — pickups, dropoffs, distance, and fare.
resource: samples.nyctaxi.trips
timestamp: '2026-08-01T04:30:00+00:00'
---

# Overview

One row per yellow-taxi trip in the Databricks sample `samples.nyctaxi` dataset.
Use this for trip counts, distance/fare distributions, and zip-level pickup/dropoff
patterns. Prefer aggregations — the table is illustrative, not a full TLC dump.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `tpep_pickup_datetime` | timestamp | When the passenger was picked up |
| `tpep_dropoff_datetime` | timestamp | When the passenger was dropped off |
| `trip_distance` | double | Trip distance in miles |
| `fare_amount` | double | Metered fare amount (USD) |
| `pickup_zip` | int | Pickup location ZIP code |
| `dropoff_zip` | int | Dropoff location ZIP code |

# Examples

```sql
-- How many trips?
SELECT COUNT(*) AS trip_count FROM samples.nyctaxi.trips

-- Average fare and distance
SELECT AVG(fare_amount) AS avg_fare, AVG(trip_distance) AS avg_miles
FROM samples.nyctaxi.trips

-- Busiest pickup ZIPs
SELECT pickup_zip, COUNT(*) AS trips
FROM samples.nyctaxi.trips
GROUP BY pickup_zip
ORDER BY trips DESC
LIMIT 10
```
