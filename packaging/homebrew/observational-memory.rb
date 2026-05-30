# typed: strict
# frozen_string_literal: true

# Formula for observational-memory.
class ObservationalMemory < Formula
  include Language::Python::Virtualenv

  desc "Local cross-agent memory and search for coding agents"
  homepage "https://github.com/intertwine/observational-memory"
  url "https://files.pythonhosted.org/packages/0b/99/23973cb24d2f24593443cfb2aab2af706aaf909abe99983e62a4a805ff0a/observational_memory-0.7.0.tar.gz"
  sha256 "d83c6a331695af8c9fb7a4254c52f0c815a0fd1c2fa69f43bb24cc94c80df240"
  license "MIT"

  depends_on "jq"
  depends_on "python@3.13"

  on_arm do
    resource "cffi" do
      url "https://files.pythonhosted.org/packages/4a/d2/a6c0296814556c68ee32009d9c2ad4f85f2707cdecfd7727951ec228005d/cffi-2.0.0-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "45d5e886156860dc35862657e1494b9bae8dfa63bf56796f2fb56e1679fc0bca"
    end

    resource "jiter" do
      url "https://files.pythonhosted.org/packages/86/59/db537c0949e83668c38481d426b9f2fd5ab758c4ee53a811dd0a510626a0/jiter-0.15.0-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "d1e7b1776f0797956c509e123d0952d10d293a9492dea9f288ab9570ec01d1a5"
    end

    resource "numpy" do
      url "https://files.pythonhosted.org/packages/1b/30/a80189bcc7f5e4258b3fbc3968d909d1756f54d023299ecc39ad6fdb9ef8/numpy-2.4.6-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "bf162abab1c1a736333192707cef898e735a5ca00f38f27eeedf44b39d9e85eb"
    end

    resource "pydantic-core" do
      url "https://files.pythonhosted.org/packages/c1/81/4fa520eaffa8bd7d1525e644cd6d39e7d60b1592bc5b516693c7340b50f1/pydantic_core-2.46.4-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "c94f0688e7b8d0a67abf40e57a7eaaecd17cc9586706a31b76c031f63df052b4"
    end
  end

  on_intel do
    resource "cffi" do
      url "https://files.pythonhosted.org/packages/4b/8d/a0a47a0c9e413a658623d014e91e74a50cdd2c423f7ccfd44086ef767f90/cffi-2.0.0-cp313-cp313-macosx_10_13_x86_64.whl"
      sha256 "00bdf7acc5f795150faa6957054fbbca2439db2f775ce831222b66f192f03beb"
    end

    resource "jiter" do
      url "https://files.pythonhosted.org/packages/e5/f4/f708c900ecee41b2025ef8413d5351e5649eb2125c506f6720cc69b06f5c/jiter-0.15.0-cp313-cp313-macosx_10_12_x86_64.whl"
      sha256 "1c11465f97e2abf45a014b83b730222f8f1c5335e802c7055a67d50de6f1f4e3"
    end

    resource "numpy" do
      url "https://files.pythonhosted.org/packages/fb/82/bdab26d7438c6791ca31b7c024ca37c1eab8b726ba236129005cd4a06e45/numpy-2.4.6-cp313-cp313-macosx_10_13_x86_64.whl"
      sha256 "511dbaf848decaaaf4b4ca48032619fb3138710c4bf7da7617765edad1ef96b0"
    end

    resource "pydantic-core" do
      url "https://files.pythonhosted.org/packages/51/a2/5d30b469c5267a17b39dec53208222f76a8d351dfac4af661888c5aee77d/pydantic_core-2.46.4-cp313-cp313-macosx_10_12_x86_64.whl"
      sha256 "5d5902252db0d3cedf8d4a1bc68f70eeb430f7e4c7104c8c476753519b423008"
    end
  end

  preserve_rpath

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/78/b6/6307fbef88d9b5ee7421e68d78a9f162e0da4900bc5f5793f6d3d0e34fb8/annotated_types-0.7.0-py3-none-any.whl"
    sha256 "1f02e8b43a8fbbc3f3e0d4f0f4bfc8131bcb4eebe8849b8e5c773f3a1c582a53"
  end

  resource "anthropic" do
    url "https://files.pythonhosted.org/packages/83/75/be0c357e33a5a56c8f9db5b4212f886138d2bf59c0952d858f6b75d710ef/anthropic-0.105.2-py3-none-any.whl"
    sha256 "e53ed5f6bf36fb1ecb9b25d8634cfd30e02fab9fb3374a0c2d5c585874757230"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/da/42/e921fccf5015463e32a3cf6ee7f980a6ed0f395ceeaa45060b61d86486c2/anyio-4.13.0-py3-none-any.whl"
    sha256 "08b310f9e24a9594186fd75b4f73f4a4152069e3853f1ed8bfbf58369f4ad708"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/59/8c/57e832b7af6d7c5abe66eb3fbe3a3a32f4d11ea23a1aa7131371035be991/certifi-2026.5.20-py3-none-any.whl"
    sha256 "3c52e209ba0a4ad7aebe60436a4ab349c39e1e602e8c134221e546902ad25897"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/c7/0d/67e5b4109ea4a837e80daa87c2c696711955e40449a97e8926672534def2/click-8.4.1-py3-none-any.whl"
    sha256 "482be17c6991b8c19c5429a1e995d9b0efdbb63172824c41f99965dc0ade8ec2"
  end

  resource "cryptography" do
    url "https://files.pythonhosted.org/packages/df/3d/01f6dd9190170a5a241e0e98c2d04be3664a9e6f5b9b872cde63aff1c3dd/cryptography-48.0.0-cp311-abi3-macosx_10_9_universal2.whl"
    sha256 "0c558d2cdffd8f4bbb30fc7134c74d2ca9a476f830bb053074498fbc86f41ed6"
  end

  resource "distro" do
    url "https://files.pythonhosted.org/packages/12/b3/231ffd4ab1fc9d679809f356cebee130ac7daa00d6d6f3206dd4fd137e9e/distro-1.9.0-py3-none-any.whl"
    sha256 "7bffd925d65168f85027d8da9af6bddab658135b840670a223589bc0c8ef02b2"
  end

  resource "docstring-parser" do
    url "https://files.pythonhosted.org/packages/a7/5f/ed01f9a3cdffbd5a008556fc7b2a08ddb1cc6ace7effa7340604b1d16699/docstring_parser-0.18.0-py3-none-any.whl"
    sha256 "b3fcbed555c47d8479be0796ef7e19c2670d428d72e96da63f3a40122860374b"
  end

  resource "h11" do
    url "https://files.pythonhosted.org/packages/04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/h11-0.16.0-py3-none-any.whl"
    sha256 "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86"
  end

  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/7e/f5/f66802a942d491edb555dd61e3a9961140fd64c90bce1eafd741609d334d/httpcore-1.0.9-py3-none-any.whl"
    sha256 "2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/2a/39/e50c7c3a983047577ee07d2a9e53faf5a69493943ec3f6a384bdc792deb2/httpx-0.28.1-py3-none-any.whl"
    sha256 "d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/de/a7/f76514cc40ad6234098ecdebda08732d75964776c51a42845b7da10649e2/idna-3.17-py3-none-any.whl"
    sha256 "466e48829084efe2548012b855df21540b96f2e20e51bd124c851536556a592c"
  end

  resource "openai" do
    url "https://files.pythonhosted.org/packages/0a/bf/ccff9be562e24207716d04ef9dc931c76aff0c89a7265da43e2104d7fe06/openai-2.38.0-py3-none-any.whl"
    sha256 "ec6661c57b2dcc47414a767e6e3335c7ed3d19c9696999283a3c82e95c756a3c"
  end

  resource "pycparser" do
    url "https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl"
    sha256 "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/fd/7b/122376b1fd3c62c1ed9dc80c931ace4844b3c55407b6fb2d199377c9736f/pydantic-2.13.4-py3-none-any.whl"
    sha256 "45a282cde31d808236fd7ea9d919b128653c8b38b393d1c4ab335c62924d9aba"
  end

  resource "rank-bm25" do
    url "https://files.pythonhosted.org/packages/2a/21/f691fb2613100a62b3fa91e9988c991e9ca5b89ea31c0d3152a3210344f9/rank_bm25-0.2.2-py3-none-any.whl"
    sha256 "7bd4a95571adadfc271746fa146a4bcfd89c0cf731e49c3d1ad863290adbe8ae"
  end

  resource "sniffio" do
    url "https://files.pythonhosted.org/packages/e9/44/75a9c9421471a6c4805dbf2356f7c181a29c1879239abab1ea2cc8f38b40/sniffio-1.3.1-py3-none-any.whl"
    sha256 "2f6da418d1f1e0fddd844478f41680e794e6051915791a034ff65e5f100525a2"
  end

  resource "tqdm" do
    url "https://files.pythonhosted.org/packages/16/e1/3079a9ff9b8e11b846c6ac5c8b5bfb7ff225eee721825310c91b3b50304f/tqdm-4.67.3-py3-none-any.whl"
    sha256 "ee1e4c0e59148062281c49d80b25b67771a127c85fc9676d3be5f243206826bf"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/18/67/36e9267722cc04a6b9f15c7f3441c2363321a3ea07da7ae0c0707beb2a9c/typing_extensions-4.15.0-py3-none-any.whl"
    sha256 "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/dc/9b/47798a6c91d8bdb567fe2698fe81e0c6b7cb7ef4d13da4114b41d239f65d/typing_inspection-0.4.2-py3-none-any.whl"
    sha256 "4ed1cacbdc298c220f1bd249ed5287caa16f34d44ef4e9c3d0cbad5b521545e7"
  end

  def install
    venv = virtualenv_create(libexec, "python3.13")
    python = Formula["python@3.13"].opt_bin/"python3.13"

    resources.each do |resource|
      wheel = buildpath/File.basename(resource.url)
      cp resource.cached_download, wheel
      system python, "-m", "pip", "--python=#{libexec/"bin/python"}", "install", "--no-deps", wheel
    end

    venv.pip_install_and_link(buildpath)
  end

  test do
    assert_match "Usage: om", shell_output("#{bin}/om --help")
    assert_match "Observational Memory Status", shell_output("#{bin}/om status")
  end
end
