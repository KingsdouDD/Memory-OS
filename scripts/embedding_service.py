#!/usr/bin/env python3
"""
Memory OS Embedding Service
自动检测GPU并生成向量嵌入

支持的后端:
- NVIDIA GPU: CUDA + llama.cpp
- Apple GPU: Metal + llama.cpp
- CPU fallback

模型: bge-m3-Q8_0.gguf
"""

import os
import sys
import json
import argparse
from pathlib import Path

# 尝试导入必要的库
try:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import Llava15ChatHandler
except ImportError:
    print("请安装 llama-cpp-python: pip install llama-cpp-python")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("请安装 numpy: pip install numpy")
    sys.exit(1)


class GPUDetector:
    """自动检测可用GPU"""

    @staticmethod
    def detect():
        """检测GPU类型并返回配置"""

        # 检查NVIDIA CUDA
        if GPUDetector._check_cuda():
            return {
                "type": "nvidia",
                "backend": "cuda",
                "n_gpu_layers": 99,  # 全部GPU加速
                "note": "使用NVIDIA CUDA加速"
            }

        # 检查Apple Metal
        if GPUDetector._check_metal():
            return {
                "type": "apple_metal",
                "backend": "metal",
                "n_gpu_layers": 99,
                "note": "使用Apple Metal加速"
            }

        # Fallback to CPU
        return {
            "type": "cpu",
            "backend": "cpu",
            "n_gpu_layers": 0,
            "note": "使用CPU计算"
        }

    @staticmethod
    def _check_cuda():
        """检查NVIDIA GPU"""
        try:
            # 方法1: 检查CUDA_VISIBLE_DEVICES
            if os.environ.get("CUDA_VISIBLE_DEVICES"):
                return True

            # 方法2: 检查nvidia-smi
            result = os.system("which nvidia-smi > /dev/null 2>&1")
            if result == 0:
                return True

            # 方法3: 尝试导入torch检测CUDA
            try:
                import torch
                return torch.cuda.is_available()
            except ImportError:
                pass

            return False
        except:
            return False

    @staticmethod
    def _check_metal():
        """检查 Apple Metal。
        🔧 2026-08-10 修复：原实现 system_profiler SPDisplaysAsics 参数写错，
        永远 grep 不到 Apple → 永远走 CPU（导致 embed daemon 91% CPU 烧电脑）。
        Apple Silicon (arm64) 一定有 Metal，直接判定。
        """
        try:
            import platform
            if platform.system() != "Darwin":
                return False
            # Apple Silicon 必定支持 Metal（M1/M2/M3/M4 都是）
            if platform.machine() == "arm64":
                return True
            # Intel Mac 老机型有 Metal 但不保证，快速用 system_profiler 确认（正确参数）
            try:
                result = os.popen("system_profiler SPDisplaysDataType 2>/dev/null | grep -i 'Metal'").read()
                return "metal" in result.lower()
            except Exception:
                return False
        except Exception:
            return False


class EmbeddingModel:
    """BGE-M3 嵌入模型"""

    def __init__(self, model_path: str, gpu_config: dict):
        self.model_path = model_path
        self.gpu_config = gpu_config
        self.llm = None
        self._load_model()

    def _load_model(self):
        """加载模型"""
        print(f"正在加载模型: {self.model_path}")
        print(f"GPU配置: {self.gpu_config['note']}")

        # 构建llama.cpp参数
        params = {
            "model_path": self.model_path,
            "n_ctx": 2048,  # 上下文长度
            "n_threads": os.cpu_count() or 4,
            "use_mmap": True,
            "use_mlock": False,
            "embedding": True,  # BGE-M3 是 embedding 模型
        }

        # 根据GPU类型设置后端
        if self.gpu_config["type"] == "nvidia":
            params["n_gpu_layers"] = self.gpu_config["n_gpu_layers"]
            params["main_gpu"] = 0
        elif self.gpu_config["type"] == "apple_metal":
            params["n_gpu_layers"] = self.gpu_config["n_gpu_layers"]
            # llama.cpp在macOS上默认使用Metal

        try:
            self.llm = Llama(**params)
            print("模型加载成功!")
        except Exception as e:
            print(f"模型加载失败: {e}")
            raise

    def encode(self, text: str) -> list:
        """生成文本嵌入向量"""
        if not self.llm:
            raise RuntimeError("模型未加载")

        # BGE-M3 使用特殊格式的prompt
        prompt = f"Represent this sentence for searching: {text}"

        # 生成嵌入
        embedding = self.llm.create_embedding(prompt)

        if isinstance(embedding, dict) and "data" in embedding:
            return embedding["data"][0]["embedding"]

        return embedding

    def encode_batch(self, texts: list) -> list:
        """批量生成嵌入向量"""
        return [self.encode(text) for text in texts]


class MemoryOSEmbedding:
    """Memory OS 嵌入服务主类"""

    def __init__(self, model_path: str = None):
        # 默认模型路径
        if model_path is None:
            model_path = "/Users/king/.openclaw/workspace/models/bge-m3-Q8_0.gguf"

        self.model_path = model_path
        self.gpu_config = GPUDetector.detect()
        self.model = None

        # 加载模型
        if os.path.exists(model_path):
            self.model = EmbeddingModel(model_path, self.gpu_config)
        else:
            print(f"警告: 模型文件不存在 {model_path}")
            print("将使用API模式或等待本地模型")

    def get_embedding(self, text: str) -> list:
        """获取单个文本的嵌入向量"""
        if self.model:
            return self.model.encode(text)
        else:
            raise RuntimeError("模型未加载，无法生成嵌入")

    def get_embeddings(self, texts: list) -> list:
        """批量获取嵌入向量"""
        if self.model:
            return self.model.encode_batch(texts)
        else:
            raise RuntimeError("模型未加载，无法生成嵌入")

    def get_status(self) -> dict:
        """获取服务状态"""
        return {
            "model_loaded": self.model is not None,
            "model_path": self.model_path,
            "gpu_config": self.gpu_config,
            "model_exists": os.path.exists(self.model_path)
        }


# CLI 入口
def main():
    parser = argparse.ArgumentParser(description="Memory OS Embedding Service")
    parser.add_argument("--model", "-m", help="模型路径")
    parser.add_argument("--text", "-t", help="要嵌入的文本")
    parser.add_argument("--batch", "-b", help="批量文件路径 (JSON格式)")
    parser.add_argument("--output", "-o", help="输出文件路径")
    parser.add_argument("--status", "-s", action="store_true", help="显示状态信息")

    args = parser.parse_args()

    # 创建服务实例
    service = MemoryOSEmbedding(args.model)

    # 显示状态
    if args.status:
        print(json.dumps(service.get_status(), indent=2, ensure_ascii=False))
        return

    # 单文本嵌入
    if args.text:
        try:
            embedding = service.get_embedding(args.text)
            result = {"text": args.text, "embedding": embedding}

            if args.output:
                with open(args.output, "w") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"结果已保存到: {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)

    # 批量嵌入
    elif args.batch:
        try:
            with open(args.batch, "r") as f:
                data = json.load(f)

            texts = data if isinstance(data, list) else data.get("texts", [])

            embeddings = service.get_embeddings(texts)
            result = {"embeddings": embeddings}

            if args.output:
                with open(args.output, "w") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"批量处理完成，结果已保存到: {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
