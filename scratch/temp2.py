import pandas as pd
df_d = pd.read_csv('data/benchmark_results_dynamic.csv')
print(f'Dynamic Avg Active Spot A: {df_d.active_spot_a.mean():.2f}')
print(f'Dynamic Avg Active Spot B: {df_d.active_spot_b.mean():.2f}')
