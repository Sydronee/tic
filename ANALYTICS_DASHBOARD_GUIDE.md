# Analytics Dashboard Guide

This guide explains the charts and NPI-related fields in `analytics.html` one by one, so you can read the dashboard without having to infer what each card is trying to show.

The dashboard is driven by the shared `v_rates` view, so most cards are different slices of the same underlying negotiated-rate rows. Filters such as payer, code system, negotiation arrangement, setting, source, and provider narrow the same base dataset before a chart is rendered.

## Core Concepts

- `rate`: the negotiated dollar amount used by most charts.
- `payer`: the insurer or payer name.
- `billing_code` and `billing_code_type`: the procedure identifier and its code system.
- `negotiation_arrangement`: a grouping label used to separate bundled, fee-for-service, or other arrangement types.
- `provider_reference_id`: a group/reference identifier for provider rows. This is not always the same thing as an NPI.
- `npi`: the actual National Provider Identifier for a provider row.
- `facility_name`: a human-readable label for the provider, often easier to scan than raw IDs.

## Executive Overview

### 1. Price distribution
Shows the overall spread of negotiated rates.

How to read it:
- A tight cluster means most prices sit near a common level.
- A long right tail means a small set of very expensive rates is pulling the distribution upward.
- If the chart is skewed, the median is usually more informative than the average.

### 2. Mix by code system
Shows how negotiated rows are distributed across billing code types.

How to read it:
- A dominant slice means that code system contributes most of the recorded prices.
- A balanced mix means the dataset is spread across multiple billing standards.
- This is useful for checking whether one code family is overwhelming the rest of the data.

### 3. Average price by payer
Compares mean negotiated rates across payers.

How to read it:
- Higher bars mean that payer tends to reimburse more across the filtered dataset.
- Large gaps often indicate payer-level pricing differences rather than just procedure mix.
- Compare with median-based views if a payer has a few extreme outliers.

### 4. Top procedures by negotiated value
Ranks procedures by total negotiated value.

How to read it:
- Large totals usually mean a combination of high price and frequent pricing activity.
- This is a volume-weighted view, so it favors procedures that matter financially across many rows.
- Use it to identify the procedures most responsible for spend concentration.

### 5. Payer price index vs. procedure median
Shows how each payer compares to the procedure-level median.

How to read it:
- Values above zero mean the payer is pricing above the median.
- Values below zero mean the payer is pricing below the median.
- This is a relative benchmark, so it is better for comparison than for absolute dollars.

## Price Analytics

### 6. Histogram
Shows the count of negotiated rates by price bucket.

How to read it:
- Tall bars indicate common price bands.
- Gaps show price ranges that rarely occur.
- Good for understanding the rough shape of the market.

### 7. Log-scale histogram
Same idea as the histogram, but on a log scale.

How to read it:
- Useful when prices span several orders of magnitude.
- Makes lower and middle ranges easier to compare when a few very large prices dominate a linear chart.
- If the linear histogram looks compressed, this view usually reveals more structure.

### 8. Kernel density estimate
Smoothed version of the price distribution.

How to read it:
- Peaks show where prices naturally cluster.
- Multiple peaks can indicate distinct pricing regimes.
- This is a smoother way to see shape than a raw histogram.

### 9. Percentile curve
Shows how rates accumulate across the sorted distribution.

How to read it:
- Steeper sections mean many rates are packed into a narrow band.
- Flatter sections mean prices are more spread out.
- Useful for spotting where the middle of the distribution sits.

### 10. Box plot by payer
Summarizes each payer’s rate distribution with median, quartiles, and outliers.

How to read it:
- The middle line is the median.
- The box shows the interquartile range.
- Long whiskers or many outliers mean a payer has highly variable pricing.

### 11. Violin plot by payer
Shows payer price distribution shape more explicitly than a box plot.

How to read it:
- Wider sections mean more rows at that price level.
- Narrow sections mean fewer observations.
- Good for seeing whether a payer has multiple price clusters.

### 12. Cumulative distribution
Shows the cumulative share of rates as prices increase.

How to read it:
- A steep climb means many values are concentrated in one area.
- A shallow climb means prices are more evenly spread.
- Helpful for answering questions like “what share of prices are below this point?”

### 13. Outlier detection
Plots rows that look unusual relative to the procedure median.

How to read it:
- High z-scores indicate rates far above the typical level for that procedure.
- Negative values mean unusually low prices.
- Use this as a triage tool, not a final verdict, because outliers can be valid but rare.

## Procedures

### 14. Top procedures by average cost
Ranks procedures by mean negotiated rate.

How to read it:
- High average cost does not always mean high spend, especially if the procedure is rare.
- This view can be distorted by outliers, so pair it with the median view.

### 15. Top procedures by median cost
Ranks procedures by median negotiated rate.

How to read it:
- Usually more robust than the average when the distribution is skewed.
- Good for identifying the typical price level for expensive procedures.

### 16. Most frequently negotiated
Shows procedures with the most observed negotiated rows.

How to read it:
- High counts mean broad payer participation or repeated entries.
- These procedures are often better candidates for market comparison because the sample is larger.

### 17. Highest price variability
Ranks procedures by coefficient of variation.

How to read it:
- Higher values mean the price is less consistent across payers.
- Very large variability can point to weak market standardization or mixed service definitions.

### 18. Negotiated value treemap
Uses area to show how total negotiated value is distributed across procedures.

How to read it:
- Large rectangles represent procedures that account for more total spend.
- Treemaps are best for concentration, not precise comparison.
- Look for a few dominating procedures versus a broad long tail.

### 19. Code system breakdown
Shows how procedures and/or value split by code system.

How to read it:
- Lets you see whether CPT, HCPCS, MS-DRG, or another system dominates the filtered results.
- Useful for understanding whether the dataset is mostly facility-style or professional-style pricing.

### 20. Cost vs. variability
Scatters median price against coefficient of variation.

How to read it:
- Top-right points are expensive and inconsistent.
- Bottom-right points are expensive but stable.
- This is a quick way to identify high-value procedures that also vary a lot by payer.

### 21. Pareto - value concentration
Shows how much total negotiated value is concentrated in the top procedures.

How to read it:
- A steep cumulative line means a small number of procedures account for a large fraction of value.
- A flatter line means spend is spread across many procedures.
- Useful for prioritizing where analysis effort will have the most impact.

### 22. Procedure ranking table
A sortable table of procedure metrics.

How to read it:
- Use it when you need exact values rather than just chart shapes.
- Sort by spend, median, average, or count depending on the question.

## Payers

### 23. Payer profile radar
Compares payer behavior across several dimensions at once.

How to read it:
- A larger overall shape usually indicates higher relative values across the displayed metrics.
- Uneven spikes show payer strengths or weaknesses in specific dimensions.
- Best used for broad payer profiling, not precise comparison.

### 24. Price index heatmap
Shows payer-by-procedure price levels relative to the median.

How to read it:
- Warmer colors usually mean above-median pricing.
- Cooler colors usually mean below-median pricing.
- Look for consistent columns or rows to spot systematic pricing patterns.

### 25. Rate volume by code system
Shows how many negotiated rows each payer has by code system.

How to read it:
- Higher bars mean a payer has more negotiated activity in that code family.
- Good for seeing whether a payer is mostly active in one type of billing code.

### 26. Price level vs. consistency
Plots average price against variability for each payer.

How to read it:
- High and stable payers sit toward the upper-left or upper-middle depending on the scale.
- Low but volatile payers sit lower or farther right.
- Helps separate expensive payers from inconsistent ones.

### 27. Procedure coverage
Shows what share of procedures each payer prices.

How to read it:
- Higher coverage means broader participation across the procedure universe.
- Lower coverage means the payer only has negotiated rows for a smaller subset.
- Coverage is important when comparing payers, because a narrow sample can be misleading.

### 28. Coefficient of variation
Shows payer-level variation in negotiated rates.

How to read it:
- Higher values mean less consistency across procedures or rows.
- Lower values mean the payer’s pricing is more uniform.
- Useful when comparing price stability between payers.

### 29. Payer -> code system flow
Shows how payer activity flows into different code systems.

How to read it:
- Thicker flows mean more negotiated rows moving from that payer into that code family.
- Good for spotting whether a payer concentrates on certain billing types.

### 30. Payer comparison table
A sortable table comparing payer metrics.

How to read it:
- Use it to compare payers on mean, median, variability, coverage, and total value.
- It is the quickest place to verify a pattern seen in the charts.

### 31. Top providers by negotiated activity
Shows providers with the most negotiated rate rows.

How to read it:
- Higher counts mean the provider appears more often in the negotiated-rate data.
- This is an activity view, not necessarily a quality or volume view.
- If a provider has a recognizable facility name, that is often easier to interpret than raw IDs.

## Benchmarks

### 32. Most expensive procedures
Ranks procedures by highest median price.

How to read it:
- Highlights procedures that are expensive in typical terms.
- Median is used so a few extreme values do not dominate the ranking.

### 33. Least expensive procedures
Ranks procedures by lowest median price.

How to read it:
- Useful for finding low-cost procedure classes or procedures with unusually low negotiated rates.
- Low cost does not automatically mean low importance; check frequency too.

### 34. Largest absolute spread
Ranks procedures by the difference between max and min prices.

How to read it:
- Big spread means payers disagree a lot or the distribution is wide.
- This can reveal procedures where negotiated prices vary most dramatically.

### 35. Average vs. median
Compares each procedure’s mean and median price.

How to read it:
- If average and median are close, the distribution is fairly balanced.
- If average is much higher than median, a few high prices are pulling the average up.

### 36. Largest interquartile range
Ranks procedures by the width of the middle 50% of prices.

How to read it:
- A wide IQR means the “typical” price range is still broad.
- Good for spotting procedures with unstable middle-market pricing.

### 37. Widest payer participation
Ranks procedures by how many payers negotiate them.

How to read it:
- High payer participation means the procedure is broadly negotiated across the market.
- These procedures are often the strongest candidates for payer-to-payer comparison.

### 38. Benchmark table
A sortable table of the benchmark metrics.

How to read it:
- Use it when you want the exact ranking behind the benchmark charts.
- Helpful for checking whether a procedure is expensive because of level, spread, or both.

## Variation

### 39. Highest payer disagreement
Shows procedures where payer prices are most dispersed.

How to read it:
- Large disagreement usually means payers are not clustering around one standard price.
- This can indicate market fragmentation or inconsistent contract structures.

### 40. Lowest payer disagreement
Shows procedures where payer prices are most aligned.

How to read it:
- Low disagreement suggests a relatively standardized market price.
- These procedures are good reference points when you want stable comparisons.

### 41. Max ÷ min price ratio
Compares the highest price to the lowest price for each procedure.

How to read it:
- A high ratio means the spread is very large even if the absolute dollar range is not huge.
- This is a strong indicator of price dispersion.

### 42. Spread vs. median
Plots spread against central price level.

How to read it:
- High-right points are both expensive and highly variable.
- High-left points are low-cost but volatile.
- Useful for separating magnitude from inconsistency.

### 43. Variation detail
A detailed table of variation metrics.

How to read it:
- Use it to inspect the exact numbers behind the variation charts.
- Best when you want a small set of procedures to investigate manually.

## Insights

This section does not have a fixed card title. It renders dynamic insight cards based on the current filters and data.

How to read it:
- Treat these cards as automatically generated findings, not permanent dashboards.
- They are useful for quick triage and for surfacing patterns that are worth a deeper look.
- The exact content can change depending on the current filter state.

## Explorer

This section is a general-purpose table/explorer area rather than a single chart.

How to read it:
- Use it to inspect rows directly.
- It is the best place to validate whether a trend seen in a chart really exists in the underlying records.
- Think of it as the “show me the raw data” panel.

## SQL Console

### 44. Run SQL
Lets you run ad hoc SQL against the loaded dataset.

How to read it:
- Use it for custom questions that are not already covered by the dashboard.
- Best for one-off checks, debugging filters, or confirming a suspected pattern.

### 45. Recent queries
Shows the SQL statements you recently ran.

How to read it:
- Useful for replaying or copying a query after you have refined it.
- Also helps you confirm which filter combination produced a given result.

## NPI And Provider Fields

### 46. Provider refs KPI
This is the top-level KPI labeled as distinct NPIs / group ids.

How to read it:
- A higher number means the filtered data touches more distinct provider references or NPI groups.
- This is a breadth metric, not a price metric.
- Use it to understand how widely provider identities are represented in the current slice.

### 47. Provider filter
The provider filter narrows results to a specific provider or facility.

How to read it:
- Select a provider when you want to ask questions about one facility or one provider group.
- If the label shows a facility name, that is usually the most readable identity to start from.
- If the label shows a number, it is often the underlying NPI or provider reference id.

### 48. Provider table
The provider table shows the provider identity fields directly.

How to read it:
- `Provider` is usually a display label derived from facility name or NPI.
- `NPI` is the actual provider identifier.
- `Reference ID` is the grouped provider reference id used by the rate records.
- `Rows` tells you how many negotiated-rate rows are linked to that provider.
- `Median`, `Avg`, and `Total` help you see whether that provider is associated with expensive, typical, or high-volume negotiated activity.

## Practical Reading Tips

- Use median-based charts when the data is skewed.
- Use spread or coefficient-of-variation charts when you care about inconsistency.
- Use the provider table when you need to connect a rate pattern back to a real NPI or facility.
- Use the explorer or SQL console when a chart suggests something interesting and you want to verify the underlying rows.
- If a chart looks empty, check the active filters first; most cards only show what survives the current filter set.
