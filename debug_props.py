#!/usr/bin/env python3
"""Debug script to find available robot properties."""
import asyncio
import yaml
from pylitterbot import Account

async def main():
    # Load config
    with open('/Users/foxyclaw/.openclaw/workspace/watchdog_services/config.yaml') as f:
        config = yaml.safe_load(f)
    
    account = Account()
    try:
        await account.connect(
            username=config['whisker']['email'],
            password=config['whisker']['password'],
            load_robots=True
        )
        
        for robot in account.robots:
            print(f"\n{'='*60}")
            print(f"Robot: {robot.name}")
            print(f"Type: {type(robot)}")
            print(f"\nAll attributes:")
            
            # List all attributes
            for attr in sorted(dir(robot)):
                if not attr.startswith('_'):
                    try:
                        val = getattr(robot, attr)
                        if not callable(val):
                            print(f"  {attr}: {val}")
                    except:
                        pass
            
            print(f"\n{'='*60}")
            
    finally:
        await account.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
