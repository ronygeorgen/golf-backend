---
description: Fix Race Conditions in Booking System
---

# Race Condition Fix Implementation Plan

## Overview
This plan addresses critical race conditions in the booking system that allow double bookings and payment conflicts.

## Root Cause
The system separates availability checking, payment, and booking creation without enforcing mutual exclusion or slot ownership. Multiple concurrent users can believe they own the same slot.

## Solution Strategy

### 1. Database-Level Constraints
- Add unique constraint to prevent overlapping bookings at database level
- Use PostgreSQL exclusion constraints or check constraints

### 2. Atomic Slot Reservation
- Use `select_for_update()` to lock rows during availability checks
- Reserve slots in TempBooking with proper locking
- Include TempBooking in availability checks

### 3. Idempotency Protection
- Add payment_id tracking to prevent duplicate webhook processing
- Implement idempotency keys for webhook calls

### 4. Expiry Enforcement
- Add background task to clean up expired TempBookings
- Enforce expiry during availability checks

### 5. Transaction Isolation
- Use proper transaction isolation levels
- Implement row-level locking with `select_for_update()`

## Implementation Steps

### Step 1: Add Database Constraint (Migration)
Create a database migration to add exclusion constraint preventing overlapping bookings for the same simulator.

### Step 2: Update TempBooking Model
- Add `payment_id` field for idempotency
- Add `status` field to track reservation state
- Add indexes for performance

### Step 3: Update Availability Checking Logic
- Use `select_for_update()` for atomic checks
- Include non-expired TempBookings in availability checks
- Lock simulator rows during check

### Step 4: Update Booking Creation Flow
- Lock slots before creating TempBooking
- Verify availability atomically
- Create TempBooking with RESERVED status

### Step 5: Update Webhook Handler
- Add idempotency check using payment_id
- Use `select_for_update()` when checking availability
- Verify TempBooking ownership before creating Booking
- Handle concurrent webhook calls gracefully

### Step 6: Add Cleanup Task
- Create management command to clean expired TempBookings
- Schedule periodic cleanup

### Step 7: Add Monitoring
- Log all race condition scenarios
- Add metrics for double booking attempts
- Alert on conflicts

## Testing Strategy
1. Concurrent booking simulation tests
2. Webhook retry tests
3. Expiry enforcement tests
4. Database constraint validation tests

## Rollback Plan
- Keep old code paths temporarily
- Feature flag for new logic
- Monitor error rates
- Quick rollback capability
