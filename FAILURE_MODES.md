# Five ways this breaks in production, and the guard for each

Each guard is in the code and has a test that proves it. The test name is given so it can be checked.

## 1. Tracking breaks and a country looks more profitable than it is
**How it happens:** a theme or app adds a second Meta pixel, or the Conversions API and the browser pixel stop sharing event ids. Meta counts every sale twice and its ROAS doubles overnight. A system that steers on platform ROAS raises the budget exactly where the data is wrong.
**Guard:** steering is judged only on Shopify revenue. Every day the system compares what each platform claims with the orders Shopify credits it. A sudden jump in that ratio freezes raises in that country and opens a task for the tracking owner. The freeze is checked again right before any write, so a raise approved earlier is still stopped. Lowering stays allowed.
**In the demo:** FR Meta shows ROAS 10.5 while Shopify says 3.7. Raises in FR are frozen and Jonas gets the task the same morning.
**Tests:** `test_catches_fr_double_pixel_and_freezes_raises`, `test_no_raises_while_frozen`, `test_freeze_is_checked_again_at_apply_time`

## 2. The platform says OK but the change did not happen
**How it happens:** the API accepts a write and then drops it, or stores a slightly different value than was sent. The system believes it changed something it did not.
**Guard:** every write is a dry run first, then the write, then a read back. A change only counts as done when the value read back from the platform matches what the dry run said the platform would store. Otherwise it is marked failed and its owner is told.
**Tests:** `test_write_that_does_not_stick_is_caught`, `test_apply_reads_back_and_undo_restores`

## 3. Someone edits the budget by hand between proposal and apply
**How it happens:** Mikkel approves a cut on Monday morning. At lunch Freja changes the same budget in Ads Manager. The approved change would overwrite her edit, and its saved "old value" would be wrong, so an undo would restore the wrong number.
**Guard:** right before writing, the system reads the live value. If it differs from the value the proposal was made against, nothing is written and the item is marked stale.
**Test:** `test_change_made_by_hand_in_the_meantime_is_not_overwritten`

## 4. The data is late
**How it happens:** an export fails overnight or the Shopify sync is behind. The system steers on a picture that is a day or more out of date.
**Guard:** every run checks each source. If yesterday is missing from any of them, or the newest data is older than 36 hours, raises are frozen everywhere and the run says which source is late.
**Test:** `test_stale_data_is_detected`

## 5. Changes pile up and run away
**How it happens:** the job runs twice, or each day nudges a budget up again, or an approval given on Monday is applied on Thursday after the situation changed. Many small, individually reasonable steps add up to a large unreviewed change.
**Guard:** the same problem is queued only once a day. Raises are capped at 20% per step and 3 days apart, counting raises made by hand in the platforms, and never in the last days of a month. Every budget goes back to plan on the first day of a month. The hard rules, the country freezes and the 10% limit on automatic lowers are all checked again at the moment of writing. An undo may skip the raise rules only on the same day as the change it undoes.
**Tests:** `test_same_problem_is_queued_once`, `test_rules_rechecked_at_apply_time`, `test_raises_three_days_apart`, `test_hand_raises_in_the_platform_count_for_the_three_day_rule`, `test_no_raise_into_next_month`, `test_new_month_resets_budgets_to_plan`, `test_undo_next_day_follows_the_ordinary_rules`, `test_only_small_lowers_run_alone`

---

## Also guarded
- **A negative blocks real buyers.** Claude only judges intent. Code refuses any negative that is not copied from the search term, contains a product word, or contains a word buyers also use, so "free plush toy sewing pattern" becomes "sewing pattern", and "fabrication" (as in "fabrication française", made in France) is never used. Every negative is a phrase match and can be undone. Tests: `test_negatives_never_contain_a_product_word`, `test_buyer_words_are_never_negatives`, `test_negative_can_be_undone_and_is_logged`
- **Claude types a number into the Monday summary.** The summary is rejected and a plain template goes out instead. Test: `test_claude_typing_numbers_in_summary_falls_back`
- **Good numbers, wrong advice.** In NL the arithmetic alone would cut one ad set by almost 80% for three weeks. The system first asks why NL is over, finds the hand raise in the change history, and proposes going back to the level that worked. It says the month still ends about 10% over, and spreads the rest evenly the next day so no single ad set is starved. Small automatic trims only touch a campaign that both overspends and earns below the floor. Tests: `test_nl_hand_raise_is_put_back_to_the_level_that_worked`, `test_after_the_revert_the_rest_of_the_overspend_is_spread_evenly`, `test_auto_trim_only_for_a_campaign_that_overspends_and_earns_too_little`

## Not built in this slice
- Real API sign in (Google Ads developer token and OAuth, Meta system user token). The mock API has the same shape.
- Approval buttons in Slack. Approval is a command today, and the recorded approver and time are the same either way.
- A scheduler. The daily and weekly runs are single commands meant for cron or a scheduled task.
- One currency. All amounts are in EUR here, so US spend would need converting at a fixed monthly rate.
