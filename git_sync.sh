# #!/bin/bash

# # 检查当前目录是否为 Git 仓库
# if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
#     echo "错误: 当前目录不是一个 Git 仓库。"
#     exit 1
# fi

# # 获取当前分支名称
# BRANCH=$(git symbolic-ref --short -q HEAD)

# echo "--- 准备提交修改到分支: $BRANCH ---"

# # 1. 展示当前状态
# git status -s

# # 2. 添加所有修改到暂存区
# git add .

# # 3. 让用户输入 Commit 信息
# # 如果直接回车，则使用默认信息
# read -p "请输入 commit 信息 (默认为 'Update at $(date +'%Y-%m-%d %H:%M')'): " msg
# if [ -z "$msg" ]; then
#     msg="Update at $(date +'%Y-%m-%d %H:%M')"
# fi

# # 4. 执行 Commit
# git commit -m "$msg"

# # 5. 询问是否推送到远程仓库
# read -p "是否推送到远程仓库 ($BRANCH)? (y/n): " confirm
# if [ "$confirm" == "y" ] || [ "$confirm" == "Y" ]; then
#     echo "正在推送..."
#     git push origin "$BRANCH"
#     if [ $? -eq 0 ]; then
#         echo "Successfully pushed!"
#     else
#         echo "Push 失败，请检查网络或冲突。"
#     fi
# else
#     echo "已在本地完成提交，未推送。"
# fi

#!/bin/bash

# 获取当前分支
BRANCH=$(git symbolic-ref --short -q HEAD)

# 如果不在 git 目录则退出
if [ -z "$BRANCH" ]; then
    echo "Error: Not a git repository."
    exit 1
fi

echo "🚀 Starting auto-sync for branch: $BRANCH"

# 添加所有修改
git add .

# 如果用户运行脚本时带了参数，则使用参数作为 commit 信息
# 否则使用默认的时间戳信息
if [ -z "$1" ]; then
    MSG="Auto-update: $(date +'%Y-%m-%d %H:%M:%S')"
else
    MSG="$1"
fi

# 执行提交
git commit -m "$MSG"

# 推送
echo "Pushing to origin $BRANCH..."
git push origin "$BRANCH"

if [ $? -eq 0 ]; then
    echo "✅ Success!"
else
    echo "❌ Push failed. Please check for conflicts or network issues."
fi