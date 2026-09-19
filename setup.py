from setuptools import setup

package_name = 'ros_media_player'

setup(
    name=package_name,
    version='0.0.1',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name, ['launch/media_player.launch.py']),
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