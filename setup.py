from setuptools import setup

setup(
    name='telegram-shop-pakasir',
    version='0.1.0',
    py_modules=['bot', 'store', 'pakasir'],
    entry_points={'console_scripts': ['toko-telegram=bot:main']},
    python_requires='>=3.10',
)
