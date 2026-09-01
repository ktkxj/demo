# -*- coding: utf-8 -*-
"""
直接使用固定的headers获取Token元数据（原命名不准确，但保持兼容）
"""

import uuid
import requests
import json


def get_tweet_data(chain_id: str, contract_address: str):
    """
    使用固定的headers发送请求
    """
    headers = {
        "sec-ch-ua-platform": "\"macOS\"",
        "referer": "https://web3.binance.com/zh-CN/trenches?chain=bsc",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec-ch-ua": "\"Not=A?Brand\";v=\"99\", \"Google Chrome\";v=\"151\", \"Chromium\";v=\"151\"",
        "content-type": "application/json",
        "sec-ch-ua-mobile": "?0",
        "x-trace-id": str(uuid.uuid4()),
        "x-ui-request-trace": str(uuid.uuid4()),
        "lang": "zh-CN"
    }

    url = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info"
    params = {"chainId": chain_id, "contractAddress": contract_address}

    print(f"\n📡 请求 CA: {contract_address} (Chain: {chain_id})")

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        print(f"   Status: {resp.status_code}")

        if resp.status_code == 200:
            return resp.json()
        else:
            return {"error": f"HTTP {resp.status_code}", "raw": resp.text[:500]}
    except Exception as e:
        return {"error": str(e)}


def main():
    """
    交互式主函数（已修复输入逻辑）
    """
    print("=" * 60)
    print("📦 获取 Binance Trenches Token 元数据")
    print("=" * 60)
    print("\n命令:")
    print("  - 输入合约地址 (CA) 获取数据，例如：0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c")
    print("  - 输入 'q' 退出")
    print("-" * 60)

    # 固定使用 BSC 主网 chainId = 56，如需其他链可自行修改
    DEFAULT_CHAIN_ID = "56"

    while True:
        try:
            user_input = input("\n📝 请输入 CA: ").strip()

            if user_input.lower() in ['q', 'quit', 'exit']:
                print("\n👋 退出程序...")
                break

            if not user_input:
                print("❌ 输入不能为空，请重新输入")
                continue

            # 调用函数，传入默认 chain_id 和用户输入的合约地址
            data = get_tweet_data(DEFAULT_CHAIN_ID, user_input)
            if data:
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print("❌ 请求失败")

        except KeyboardInterrupt:
            print("\n\n👋 退出程序...")
            break
        except Exception as e:
            print(f"❌ 发生错误: {e}")


if __name__ == "__main__":
    main()
