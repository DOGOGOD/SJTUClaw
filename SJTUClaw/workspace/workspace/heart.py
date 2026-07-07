import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# 心形曲线参数方程: x = 16 sin^3 t, y = 13 cos t - 5 cos 2t - 2 cos 3t - cos 4t
t = np.linspace(0, 2 * np.pi, 1000)
x = 16 * np.sin(t)**3
y = 13 * np.cos(t) - 5 * np.cos(2*t) - 2 * np.cos(3*t) - np.cos(4*t)

plt.figure(figsize=(8, 6))
plt.plot(x, y, color='red', linewidth=3)
plt.fill(x, y, color='hotpink', alpha=0.6)
plt.axis('equal')
plt.axis('off')
plt.savefig('heart.png', dpi=150, bbox_inches='tight')
print('heart.png saved!')
