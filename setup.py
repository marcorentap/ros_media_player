from setuptools import setup
import os
import glob

package_name = 'ros_media_player'

# Install the built frontend (frontend/dist) into the package's share dir so
# the node can serve it in production without a bundler. Guarded: colcon
# succeeds (serving the hint page) if the frontend hasn't been built yet.
#
# setuptools ``data_files`` installs each tuple flattened (basename only), so
# it would drop the ``assets/`` subdirectory. We therefore emit one tuple per
# directory: index.html lands in web/, hashed assets in web/assets/.
web_root_files = [os.path.join('frontend/dist', 'index.html')]
web_asset_files = sorted(
    f for f in glob.glob('frontend/dist/assets/**/*', recursive=True)
    if os.path.isfile(f)
)

setup(
    name=package_name,
    version='0.0.1',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name, ['launch/media_player.launch.py']),
        ('share/' + package_name + '/web', web_root_files),
        ('share/' + package_name + '/web/assets', web_asset_files),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='marcorentap',
    maintainer_email='marcorentap@example.com',
    description='Media player ROS2 Python node + Vite/React web backend.',
    license='MIT',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'media_player_node = ros_media_player.media_player_node:main',
        ],
    },
)