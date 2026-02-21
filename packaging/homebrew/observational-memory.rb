# typed: strict
# frozen_string_literal: true

# Formula for observational-memory.
class ObservationalMemory < Formula
  include Language::Python::Virtualenv

  desc "Cross-agent observational memory for Claude Code and Codex CLI"
  homepage "https://github.com/intertwine/observational-memory"
  url "https://files.pythonhosted.org/packages/8a/33/db7efdc828f8b3ab1f6326b3e8dd47629b372bb18695ddbddf1bf392334a/observational_memory-0.1.1.tar.gz"
  sha256 "2529fc28ffd7867f54724bbe492ff14abcbed017988e1637b4b5f1cd890425b6"
  license "MIT"

  depends_on "jq"
  depends_on "python@3.13"

  on_arm do
    resource "jiter" do
      url "https://files.pythonhosted.org/packages/7c/02/be5b870d1d2be5dd6a91bdfb90f248fbb7dcbd21338f092c6b89817c3dbf/jiter-0.13.0-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "f556aa591c00f2c45eb1b89f68f52441a016034d18b65da60e2d2875bbbf344a"
    end

    resource "numpy" do
      url "https://files.pythonhosted.org/packages/09/f0/817d03a03f93ba9c6c8993de509277d84e69f9453601915e4a69554102a1/numpy-2.4.2-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "bd3a7a9f5847d2fb8c2c6d1c862fa109c31a9abeca1a3c2bd5a64572955b2979"
    end

    resource "pydantic-core" do
      url "https://files.pythonhosted.org/packages/94/02/abfa0e0bda67faa65fef1c84971c7e45928e108fe24333c81f3bfe35d5f5/pydantic_core-2.41.5-cp313-cp313-macosx_11_0_arm64.whl"
      sha256 "112e305c3314f40c93998e567879e887a3160bb8689ef3d2c04b6cc62c33ac34"
    end
  end

  on_intel do
    resource "jiter" do
      url "https://files.pythonhosted.org/packages/91/9c/7ee5a6ff4b9991e1a45263bfc46731634c4a2bde27dfda6c8251df2d958c/jiter-0.13.0-cp313-cp313-macosx_10_12_x86_64.whl"
      sha256 "1f8a55b848cbabf97d861495cd65f1e5c590246fabca8b48e1747c4dfc8f85bf"
    end

    resource "numpy" do
      url "https://files.pythonhosted.org/packages/a1/22/815b9fe25d1d7ae7d492152adbc7226d3eff731dffc38fe970589fcaaa38/numpy-2.4.2-cp313-cp313-macosx_10_13_x86_64.whl"
      sha256 "25f2059807faea4b077a2b6837391b5d830864b3543627f381821c646f31a63c"
    end

    resource "pydantic-core" do
      url "https://files.pythonhosted.org/packages/87/06/8806241ff1f70d9939f9af039c6c35f2360cf16e93c2ca76f184e76b1564/pydantic_core-2.41.5-cp313-cp313-macosx_10_12_x86_64.whl"
      sha256 "941103c9be18ac8daf7b7adca8228f8ed6bb7a1849020f643b3a14d15b1924d9"
    end
  end

  preserve_rpath

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/78/b6/6307fbef88d9b5ee7421e68d78a9f162e0da4900bc5f5793f6d3d0e34fb8/annotated_types-0.7.0-py3-none-any.whl"
    sha256 "1f02e8b43a8fbbc3f3e0d4f0f4bfc8131bcb4eebe8849b8e5c773f3a1c582a53"
  end

  resource "anthropic" do
    url "https://files.pythonhosted.org/packages/5f/75/b9d58e4e2a4b1fc3e75ffbab978f999baf8b7c4ba9f96e60edb918ba386b/anthropic-0.83.0-py3-none-any.whl"
    sha256 "f069ef508c73b8f9152e8850830d92bd5ef185645dbacf234bb213344a274810"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/38/0e/27be9fdef66e72d64c0cdc3cc2823101b80585f8119b5c112c2e8f5f7dab/anyio-4.12.1-py3-none-any.whl"
    sha256 "d405828884fc140aa80a3c667b8beed277f1dfedec42ba031bd6ac3db606ab6c"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/e6/ad/3cc14f097111b4de0040c83a525973216457bbeeb63739ef1ed275c1c021/certifi-2026.1.4-py3-none-any.whl"
    sha256 "9943707519e4add1115f44c2bc244f782c0249876bf51b6599fee1ffbedd685c"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/98/78/01c019cdb5d6498122777c1a43056ebb3ebfeef2076d9d026bfe15583b2b/click-8.3.1-py3-none-any.whl"
    sha256 "981153a64e25f12d547d3426c367a4857371575ee7ad18df2a6183ab0545b2a6"
  end

  resource "distro" do
    url "https://files.pythonhosted.org/packages/12/b3/231ffd4ab1fc9d679809f356cebee130ac7daa00d6d6f3206dd4fd137e9e/distro-1.9.0-py3-none-any.whl"
    sha256 "7bffd925d65168f85027d8da9af6bddab658135b840670a223589bc0c8ef02b2"
  end

  resource "docstring-parser" do
    url "https://files.pythonhosted.org/packages/55/e2/2537ebcff11c1ee1ff17d8d0b6f4db75873e3b0fb32c2d4a2ee31ecb310a/docstring_parser-0.17.0-py3-none-any.whl"
    sha256 "cf2569abd23dce8099b300f9b4fa8191e9582dda731fd533daf54c4551658708"
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
    url "https://files.pythonhosted.org/packages/0e/61/66938bbb5fc52dbdf84594873d5b51fb1f7c7794e9c0f5bd885f30bc507b/idna-3.11-py3-none-any.whl"
    sha256 "771a87f49d9defaf64091e6e6fe9c18d4833f140bd19464795bc32d966ca37ea"
  end

  resource "openai" do
    url "https://files.pythonhosted.org/packages/cc/56/0a89092a453bb2c676d66abee44f863e742b2110d4dbb1dbcca3f7e5fc33/openai-2.21.0-py3-none-any.whl"
    sha256 "0bc1c775e5b1536c294eded39ee08f8407656537ccc71b1004104fe1602e267c"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/5a/87/b70ad306ebb6f9b585f114d0ac2137d792b48be34d732d60e597c2f8465a/pydantic-2.12.5-py3-none-any.whl"
    sha256 "e561593fccf61e8a20fc46dfc2dfe075b8be7d0188df33f221ad1f0139180f9d"
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

  resource "observational-memory-wheel" do
    url "https://files.pythonhosted.org/packages/47/eb/6028838b6eaa63444dd4897b4e1d86c2e3ba1768262e8f4d992266b2dee0/observational_memory-0.1.1-py3-none-any.whl"
    sha256 "8d9ae2db4c80a22999bd6411c71ca94585e6e265394e5d0cccb4418468976bbd"
  end

  def install
    virtualenv_create(libexec, "python3.13")
    python = Formula["python@3.13"].opt_bin/"python3.13"

    resources.each do |resource|
      next if resource.name == "observational-memory-wheel"

      wheel = buildpath/File.basename(resource.url)
      cp resource.cached_download, wheel
      system python, "-m", "pip", "--python=#{libexec/"bin/python"}", "install", "--no-deps", wheel
    end

    root_wheel = buildpath/"observational-memory-wheel.whl"
    cp resource("observational-memory-wheel").cached_download, root_wheel
    system python, "-m", "pip", "--python=#{libexec/"bin/python"}", "install", "--no-deps", root_wheel
    bin.install_symlink libexec/"bin/om"
  end

  test do
    assert_match "Usage: om", shell_output("#{bin}/om --help")
    assert_match "Observational Memory Status", shell_output("#{bin}/om status")
  end
end
