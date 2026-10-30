import asyncio
import os

from dotenv import load_dotenv
from viam.robot.client import RobotClient

load_dotenv()


async def connect():
    opts = RobotClient.Options.with_api_key(
        api_key=os.environ["VIAM_API_KEY"],
        api_key_id=os.environ["VIAM_API_KEY_ID"],
    )
    return await RobotClient.at_address(os.environ["VIAM_MACHINE_ADDRESS"], opts)


async def main():
    machine = await connect()
    print("Connected to", os.environ["VIAM_MACHINE_ADDRESS"])
    print("Resources:")
    for name in machine.resource_names:
        print(f"  {name.namespace}:{name.type}:{name.subtype}/{name.name}")
    await machine.close()


if __name__ == "__main__":
    asyncio.run(main())
