from git import Repo

def clone_repo(repo_url, local_path):
    Repo.clone_from(repo_url, local_path)

# 示例
clone_repo("https://github.com/用户名/仓库名.git", "./my_local_repo")
