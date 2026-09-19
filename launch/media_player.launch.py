import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("host", default_value="0.0.0.0"),
        DeclareLaunchArgument("port", default_value="8080"),
        DeclareLaunchArgument("command_topic", default_value="/media_player/commands"),
        DeclareLaunchArgument("data_dir", default_value="~/.ros/media_player"),
        Node(
            package="ros_media_player",
            executable="media_player_node",
            name="media_player",
            output="screen",
            parameters=[{
                "host": LaunchConfiguration("host"),
                "port": LaunchConfiguration("port"),
                "command_topic": LaunchConfiguration("command_topic"),
                "data_dir": LaunchConfiguration("data_dir"),
            }],
        ),
    ])